"""Document upload → extracted text for chat context.

A member can attach a PDF/DOCX/TXT/MD/CSV to a conversation. This
module extracts text from it synchronously on the coordinator (CPU-
bound parsing, not model inference — no worker/queue involved) and
mirrors the pattern the search tool already uses for context:
prepend extracted content to the prompt as fenced context, then
dispatch a normal chat job. No new worker capability is needed; any
existing chat-capable contributor serves it.

Storage design (owner's call, 2026-08-24): the raw uploaded file is
never written to disk here — ``extract_text`` parses straight off the
in-memory bytes read from the request's spooled upload stream, and
those bytes fall out of scope (nothing references them) the moment
this module's request handler returns. Only the EXTRACTED TEXT is
persisted, in the new ``uploads`` table (see coordinator/db.py),
riding along with the same plaintext-at-rest retention every other
stored prompt already has — see docs/project-gaps.md "Stored prompt
history is plaintext at rest" for the encryption plan that will
eventually cover this column too; there's no separate carve-out for
uploads to keep track of later.

Attachment is per-conversation and sticky: every upload for a
conversation is folded into a <<document>> fence prepended to that
conversation's *future* chat turns (see build_document_context and
its call site in coordinator/main.py's /generate), not just the
turn it was attached during. Two independent caps keep this from
inflating prompt cost without bound — MAX_UPLOAD_EXTRACTED_CHARS
per file (applied here, at extraction time) and
MAX_UPLOAD_CONTEXT_CHARS combined across every upload folded into one
fence (applied in build_document_context, newest-file-first so a
just-attached file always makes the cut). Both truncate with an
explicit marker — never silently.

Explicitly out of scope for v1 (matches docs/project-gaps.md's own
cut for this feature): image-only/scanned PDFs (needs OCR), and real
computation over structured data (e.g. "what's the average of column
X") — that needs a code-execution sandbox, not text-prepend. CSVs
get a compact text description (columns, row count, a preview), not
full-fidelity analysis.
"""
from __future__ import annotations

import io
import logging
import time
import uuid
from typing import Iterable, Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from shared.config import (
    MAX_UPLOAD_BYTES,
    MAX_UPLOAD_CONTEXT_CHARS,
    MAX_UPLOAD_EXTRACTED_CHARS,
)

log = logging.getLogger("coordinator.uploads")

# Matched on the filename extension rather than the browser-supplied
# Content-Type, which is inconsistent across browsers/OSes (.md and
# .csv in particular arrive under several different MIME types
# depending on the sender).
SUPPORTED_EXTENSIONS = frozenset({"pdf", "docx", "txt", "md", "csv"})


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(pages).strip()


def _extract_docx(data: bytes) -> str:
    from docx import Document

    doc = Document(io.BytesIO(data))
    return "\n".join(p.text for p in doc.paragraphs).strip()


def _extract_plain_text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace").strip()


def _extract_csv(data: bytes) -> str:
    """Compact description, not a full-fidelity dump — the deliberately
    simple v1 cut. See the module docstring."""
    import pandas as pd

    df = pd.read_csv(io.BytesIO(data))
    lines = [
        f"CSV with {len(df)} rows and {len(df.columns)} columns.",
        f"Columns: {', '.join(str(c) for c in df.columns)}",
        "",
        "Preview (first 20 rows):",
        df.head(20).to_string(index=False),
    ]
    return "\n".join(lines).strip()


def extract_text(filename: str, data: bytes) -> tuple[str, bool]:
    """Returns (text, truncated). Raises ValueError on an unsupported
    extension or unparseable content — the router turns that into a
    400 rather than a 500, since it's a bad-input case, not a bug."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"unsupported file type .{ext or '?'} — supported: "
            + ", ".join(sorted(SUPPORTED_EXTENSIONS))
        )
    try:
        if ext == "pdf":
            text = _extract_pdf(data)
        elif ext == "docx":
            text = _extract_docx(data)
        elif ext == "csv":
            text = _extract_csv(data)
        else:  # txt, md
            text = _extract_plain_text(data)
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001 — parser libs raise all sorts
        raise ValueError(f"couldn't read {filename}: {e}") from e
    if not text:
        raise ValueError(f"{filename} has no extractable text")
    return _truncate(text, MAX_UPLOAD_EXTRACTED_CHARS)


def build_document_context(upload_rows: Iterable) -> Optional[str]:
    """Fold a conversation's uploads into one <<document>> fence for
    the worker-facing prompt. ``upload_rows`` is oldest-first (as
    returned by db.list_uploads); walked newest-first here so a just-
    attached file always makes the combined-budget cut even when
    older attachments don't, then restored to chronological order for
    readability. Returns None when there's nothing to attach."""
    rows = list(upload_rows)
    if not rows:
        return None
    parts: list[str] = []
    budget = MAX_UPLOAD_CONTEXT_CHARS
    omitted = 0
    for row in reversed(rows):
        if budget <= 0:
            omitted += 1
            continue
        text = row["extracted_text"]
        filename = str(row["filename"]).replace('"', "'")
        piece = text[:budget]
        cut_here = len(piece) < len(text)
        budget -= len(piece)
        marker = " truncated" if (cut_here or row["truncated"]) else ""
        parts.append(
            f'<<document filename="{filename}"{marker}>>\n{piece}\n<</document>>'
        )
    if omitted:
        # Represents the oldest attachments (budget is spent
        # newest-first above) — placed first after the reverse below,
        # reading naturally as "N older files omitted" ahead of the
        # documents that did make it in, oldest to newest.
        parts.append(
            f"<<document>>\n[{omitted} older attached file"
            f"{'s' if omitted != 1 else ''} omitted — over context budget]\n"
            "<</document>>"
        )
    parts.reverse()
    return "\n\n".join(parts)


def _require_conversation_owner(request: Request, conv_row) -> None:
    """Duplicated from coordinator.main (not imported) to avoid a
    circular import — main.py imports this module to mount its
    router. Keep in sync if the ownership rule there changes."""
    from shared.auth import AUTH_ENABLED

    if not AUTH_ENABLED:
        return
    member = getattr(request.state, "member", None)
    if member is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    owner = conv_row["owner_member_id"]
    if owner is not None and owner != member.member_id and member.role != "admin":
        # 404, not 403 — don't confirm the conversation exists to a
        # non-owner. Matches _require_conversation_owner in main.py.
        raise HTTPException(status_code=404, detail="conversation not found")


def build_router(db) -> APIRouter:
    """Returns the APIRouter for /uploads/*. db is captured in the
    closure — same reasoning as notifications.build_router."""
    router = APIRouter(prefix="/uploads", tags=["uploads"])

    @router.post("")
    async def create_upload(
        request: Request,
        conversation_id: str = Form(...),
        file: UploadFile = File(...),
    ):
        conv_row = db.get_conversation(conversation_id)
        if conv_row is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        _require_conversation_owner(request, conv_row)

        # Read with a +1-byte overread so an exactly-at-the-limit file
        # isn't mistaken for over-limit, and a wildly oversized one
        # doesn't get fully buffered before we notice.
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await file.read(1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"file too large — {MAX_UPLOAD_BYTES} byte limit",
                )
        data = b"".join(chunks)
        if not data:
            raise HTTPException(status_code=400, detail="empty file")

        filename = file.filename or "upload"
        try:
            text, truncated = extract_text(filename, data)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        member = getattr(request.state, "member", None)
        upload_id = "up_" + uuid.uuid4().hex[:12]
        db.insert_upload(
            upload_id=upload_id,
            conversation_id=conversation_id,
            member_id=member.member_id if member is not None else None,
            filename=filename,
            content_type=file.content_type,
            extracted_text=text,
            truncated=truncated,
            created_at=time.time(),
        )
        log.info(
            "upload extracted",
            extra={
                "event": "upload_extracted",
                "conversation_id": conversation_id,
                # NOT "filename" — that key collides with LogRecord's
                # own reserved attribute of the same name (the source
                # file of the log call) and raises KeyError at log time.
                "upload_filename": filename,
                "char_count": len(text),
                "truncated": truncated,
            },
        )
        return {
            "upload_id": upload_id,
            "filename": filename,
            "char_count": len(text),
            "truncated": truncated,
        }

    @router.get("/{conversation_id}")
    def list_uploads_for_conversation(conversation_id: str, request: Request):
        conv_row = db.get_conversation(conversation_id)
        if conv_row is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        _require_conversation_owner(request, conv_row)
        rows = db.list_uploads(conversation_id)
        return {
            "uploads": [
                {
                    "upload_id": r["upload_id"],
                    "filename": r["filename"],
                    "char_count": r["char_count"],
                    "truncated": bool(r["truncated"]),
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
        }

    return router
