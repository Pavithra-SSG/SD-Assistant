"""AttachmentService: screenshots employees attach in chat or on the form.

Every upload is
  1. checked: PNG, JPEG or WebP only (by content, not file name), at most ATTACHMENT_MAX_MB, at most 40
     megapixels (blocks "decompression bomb" images built to exhaust memory);
  2. re-saved as a fresh PNG, which drops hidden metadata such as a phone photo's GPS location;
  3. read with OCR (RapidOCR, runs on this server: screenshots never leave the company);
  4. cleaned: any line that shows a password, code or token is blurred in the stored image and hidden in the
     stored text. The original upload is never kept;
  5. scanned for error codes (0x800CCC0E, error 809, AADSTS50076…) that help triage.
The text goes into the normal triage as extra context, so Jev still makes every decision. Files live on disk
(ATTACHMENT_DIR), not in the database, and are deleted after ATTACHMENT_RETENTION_DAYS; the row stays as a
record that a screenshot existed.
"""
from __future__ import annotations

import hashlib
import io
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import config
from ..store import Store, now

ALLOWED = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
MAX_PIXELS = 40_000_000
MAX_SIDE = 1800  # larger screenshots are scaled down first: faster OCR, same readable text
UPSCALE_BELOW = 1400  # smaller ones are read at 2x (see _read)
# words that mark the line an employee (and triage) cares about, strongest first
_PROBLEM_WORDS = [(3, re.compile(r"(?i)\b(fail(ed|ure)?|error|denied|refused|unable|can(no|')t|couldn't|timed? ?out|"
                                 r"not (found|responding|connected|available|recogni[sz]ed)|expired|invalid|blocked|"
                                 r"locked|disconnected|unreachable|crash(ed)?|stopped)\b")),
                  (1, re.compile(r"(?i)\b(warning|problem|issue|retry|try again|check|could not)\b"))]
_SECRET_LINE = re.compile(r"(?i)\b(password|passcode|passwd|pwd|otp|one[- ]time|verification code|security code|"
                          r"pin|api[_ -]?key|secret|token)\b\s*[:=]?\s*\S{4,}")
_ERROR_CODES = [
    re.compile(r"\b0x[0-9A-Fa-f]{6,8}\b"),
    re.compile(r"(?i)\berror(?:\s+code)?\s*[:#]?\s*(\d{3,5})\b"),
    re.compile(r"\b(AADSTS\d{5,6})\b"),
    re.compile(r"(?i)\b(HTTP\s*[45]\d\d)\b"),
]
_ocr_lock = threading.Lock()
_ocr_engine = None


class AttachmentError(Exception):
    status_code = 422


def _engine():
    """RapidOCR is loaded once per process, on first use (about a second)."""
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr_engine = RapidOCR()
    return _ocr_engine


def error_codes(text: str) -> list[str]:
    found = []
    for rx in _ERROR_CODES:
        for m in rx.finditer(text):
            code = m.group(1) if m.groups() else m.group(0)
            if code.lower() not in (c.lower() for c in found):
                found.append(code)
    return found[:5]


class AttachmentService:
    def __init__(self, store: Store, ocr=None):
        self.store = store
        self.dir = Path(config.ATTACHMENT_DIR)
        self._ocr = ocr  # tests inject a fake reader

    def _read(self, image) -> list[tuple[list, str, float]]:
        if not config.OCR_ENABLED:
            return []
        import numpy as np
        from PIL import Image
        # small screenshots have tiny text: OCR then drops the spaces ("Failed toconnect"). Reading a 2x copy
        # fixes that (~1 s extra); boxes are scaled back so blurring still lands on the original image.
        scale = 2 if max(image.size) < UPSCALE_BELOW else 1
        src = image.resize((image.width * scale, image.height * scale), Image.Resampling.LANCZOS) if scale > 1 else image
        with _ocr_lock:  # the ONNX session isn't shared safely across threads
            result, _ = (self._ocr or _engine())(np.asarray(src))
        return [([[p[0] / scale, p[1] / scale] for p in r[0]], r[1], float(r[2])) for r in (result or [])]

    def process(self, data: bytes, filename: str, uploaded_by: str, session_id: str | None = None,
                ticket_id: str | None = None) -> dict:
        from PIL import Image, ImageFilter, ImageOps
        if len(data) > config.ATTACHMENT_MAX_MB * 1024 * 1024:
            raise AttachmentError(f"That file is larger than {config.ATTACHMENT_MAX_MB} MB. Please send a smaller "
                                  "screenshot.")
        Image.MAX_IMAGE_PIXELS = MAX_PIXELS
        try:
            probe = Image.open(io.BytesIO(data))
            fmt = probe.format
            probe.verify()  # structural check; a new handle is needed afterwards
            image = Image.open(io.BytesIO(data))
            image.load()
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as e:
            raise AttachmentError("That image is too large to process.") from e
        except Exception as e:  # noqa: BLE001 - anything Pillow can't read is rejected the same way
            raise AttachmentError("That file isn't an image I can read. Please attach a PNG or JPG screenshot.") from e
        if fmt not in ALLOWED:
            raise AttachmentError("Please attach a PNG, JPG or WebP screenshot.")
        image = ImageOps.exif_transpose(image).convert("RGB")  # phone photos: right way up, then metadata dropped
        if max(image.size) > MAX_SIDE:
            image.thumbnail((MAX_SIDE, MAX_SIDE))

        lines = self._read(image)
        kept, blurred = [], 0
        for box, text, conf in lines:
            if _SECRET_LINE.search(text):
                xs, ys = [p[0] for p in box], [p[1] for p in box]
                area = (max(0, int(min(xs)) - 4), max(0, int(min(ys)) - 4),
                        min(image.width, int(max(xs)) + 4), min(image.height, int(max(ys)) + 4))
                image.paste(image.crop(area).filter(ImageFilter.GaussianBlur(12)), area)
                blurred += 1
                kept.append(("[hidden: looked like a password or code]", conf))
            else:
                kept.append((text, conf))
        text = "\n".join(t for t, _ in kept)
        confidence = round(sum(c for _, c in kept) / len(kept), 3) if kept else 0.0

        att_id = uuid.uuid4().hex
        self.dir.mkdir(parents=True, exist_ok=True)
        out = io.BytesIO()
        image.save(out, "PNG", optimize=True)
        (self.dir / f"{att_id}.png").write_bytes(out.getvalue())
        delete_after = (datetime.now(timezone.utc) + timedelta(days=config.ATTACHMENT_RETENTION_DAYS)).isoformat(
            timespec="seconds")
        codes = error_codes(text)
        self.store.execute(
            "INSERT INTO attachments (id,ticket_id,session_id,uploaded_by,filename,content_type,size_bytes,sha256,"
            "width,height,ocr_text,ocr_confidence,error_codes,secrets_blurred,status,created_at,delete_after) "
            "VALUES (?,?,?,?,?,'image/png',?,?,?,?,?,?,?,?,'stored',?,?)",
            (att_id, ticket_id, session_id, uploaded_by, Path(filename or "screenshot").name[:120], len(out.getvalue()),
             hashlib.sha256(out.getvalue()).hexdigest(), image.width, image.height, text[:4000], confidence,
             ",".join(codes), blurred, now(), delete_after))
        self.store.log_event("Attachment added", {"detail": f"screenshot {att_id[:8]} · {len(kept)} text lines · "
                                                            f"{blurred} secret(s) blurred"},
                             ticket_id=ticket_id, session_id=session_id, actor=uploaded_by)
        return self.get(att_id)

    # ---------------------------------------------------------------- reading back
    def get(self, att_id: str) -> dict | None:
        r = self.store.one("SELECT * FROM attachments WHERE id=?", (att_id,))
        if not r:
            return None
        return {**r, "error_codes": [c for c in (r["error_codes"] or "").split(",") if c],
                "readable": bool(r["ocr_text"]) and (r["ocr_confidence"] or 0) >= config.OCR_MIN_CONFIDENCE,
                "available": r["deleted_at"] is None}

    def for_ticket(self, ticket_id: str, session_id: str | None) -> list[dict]:
        rows = self.store.query("SELECT id FROM attachments WHERE ticket_id=?" +
                                (" OR (session_id=? AND ticket_id IS NULL)" if session_id else "") +
                                " ORDER BY created_at", (ticket_id, session_id) if session_id else (ticket_id,))
        return [self.get(r["id"]) for r in rows]

    def path(self, att: dict) -> Path | None:
        p = self.dir / f"{att['id']}.png"
        return p if att["deleted_at"] is None and p.exists() else None

    def link_session(self, session_id: str, ticket_id: str) -> None:
        """Screenshots sent before the ticket existed belong to it."""
        self.store.execute("UPDATE attachments SET ticket_id=? WHERE session_id=? AND ticket_id IS NULL",
                           (ticket_id, session_id))

    # ---------------------------------------------------------------- retention
    def purge_expired(self) -> int:
        """Delete files past their retention date. The row stays (without the text) as a record."""
        rows = self.store.query("SELECT id FROM attachments WHERE deleted_at IS NULL AND delete_after<?", (now(),))
        for r in rows:
            (self.dir / f"{r['id']}.png").unlink(missing_ok=True)
            self.store.execute("UPDATE attachments SET deleted_at=?, ocr_text=NULL, status='deleted' WHERE id=?",
                               (now(), r["id"]))
        return len(rows)


def problem_lines(text: str) -> list[str]:
    """The lines that describe what went wrong, most telling first; [] when nothing looks like a problem.
    Screenshots usually lead with a window title ("Connection Checker"), which says nothing about the fault."""
    scored = []
    for i, ln in enumerate(text.splitlines()):
        if len(ln) < 8 or ln.startswith("[hidden"):
            continue
        score = sum(w * len(rx.findall(ln)) for w, rx in _PROBLEM_WORDS) + (3 if error_codes(ln) else 0)
        if score:
            scored.append((-score, i, ln))
    return [ln for _, _, ln in sorted(scored)]


def summary_for_bot(atts: list[dict]) -> tuple[str, str]:
    """(extra context for triage, what the bot tells the employee it read)."""
    context, notes = [], []
    for a in atts:
        if a["readable"]:
            # the "[hidden: …password…]" placeholder must not reach triage: the word itself would steer the
            # category towards passwords (seen in testing: a VPN error was offered a password reset)
            lines = [ln for ln in (a["ocr_text"] or "").splitlines() if not ln.startswith("[hidden")]
            key = problem_lines(a["ocr_text"] or "")
            # the error lines lead, so they survive the length cap and triage weighs them first
            context.append(("Error shown: " + " | ".join(key[:3]) + "\n" if key else "") + "\n".join(lines)[:800])
            if a["error_codes"]:
                notes.append("I can see " + " and ".join(f"**{c}**" for c in a["error_codes"][:2]) +
                             " in your screenshot.")
            if key and not (a["error_codes"] and any(c in key[0] for c in a["error_codes"])):
                notes.append(f"Your screenshot says _\"{key[0][:140]}\"_.")
            elif not key and not a["error_codes"]:
                first = next((ln for ln in lines if len(ln) >= 8), "")
                shows = f"Your screenshot shows _\"{first[:90]}\"_, but" if first else "I've read your screenshot, but"
                notes.append(f"{shows} I couldn't spot an error message in it, so I'll go by what you've told me. "
                             "If there is an error on screen, please type it exactly as it appears.")
        elif not (a["ocr_text"] or "").strip():
            notes.append("I couldn't find any text in that image. If you meant to show an error, a screenshot "
                         "works best (on Windows press **Win + Shift + S**), or just type the message you see.")
        else:
            notes.append("I couldn't read your screenshot clearly (photos of a screen are often hard to read). "
                         "If there's an error message, could you type it exactly as it appears?")
        if a["secrets_blurred"]:
            notes.append("🔒 Your screenshot showed what looked like a password or code, so I've blurred it before "
                         "saving. If it was a real password, please change it.")
    return "\n".join(context), " ".join(notes)
