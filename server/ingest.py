"""Ingest endpoint: receives batched messages from the capture extension.

POST /ingest   shared-secret auth -> validate -> dedupe on message id -> SQLite
GET  /health   liveness + basic counters (no auth, exposes no message content)

Run:  .venv/bin/uvicorn ingest:app --host 0.0.0.0 --port 8787
"""

import hmac
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import yaml
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
CONFIG = yaml.safe_load((BASE_DIR / "config.yaml").read_text())

INGEST_SECRET = os.environ.get("INGEST_SECRET", "")

log = logging.getLogger("ingest")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

import db  # noqa: E402


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- request models ----------------------------------------------------------

_S = CONFIG["server"]


class Reply(BaseModel):
    parentId: str | None = None
    preview: str | None = None


class CaptureMessage(BaseModel):
    messageId: str = Field(pattern=r"^\d{1,25}$")
    channelId: str = Field(pattern=r"^\d{1,25}$")
    author: str | None = Field(default=None, max_length=200)
    timestamp: str | None = Field(default=None, max_length=64)
    timestampApprox: bool = False
    content: str = ""
    embeds: list[str] = []
    links: list[str] = []
    reply: Reply | None = None
    capturedAt: str = Field(max_length=64)
    edited: bool = False
    selectorVersion: str | None = Field(default=None, max_length=32)

    def clamped(self) -> "CaptureMessage":
        """Apply the size caps from config; never reject on oversize, truncate."""
        self.content = self.content[: _S["max_content_chars"]]
        self.embeds = [e[: _S["max_embed_chars"]] for e in self.embeds[: _S["max_embeds"]]]
        self.links = [l[:2000] for l in self.links[: _S["max_links"]]]
        if self.reply and self.reply.preview:
            self.reply.preview = self.reply.preview[:1000]
        return self


class Batch(BaseModel):
    messages: list[CaptureMessage]
    sentAt: str | None = None


# --- auth --------------------------------------------------------------------


def require_auth(x_capture_auth: Annotated[str, Header()] = "") -> None:
    if not INGEST_SECRET:
        raise HTTPException(503, "server has no INGEST_SECRET configured")
    if not hmac.compare_digest(x_capture_auth, INGEST_SECRET):
        raise HTTPException(401, "bad auth")


# --- app ---------------------------------------------------------------------

app = FastAPI(title="discord-signal ingest")


@app.post("/ingest", dependencies=[Depends(require_auth)])
def ingest(batch: Batch) -> dict:
    if len(batch.messages) > _S["max_batch"]:
        raise HTTPException(413, f"batch too large (max {_S['max_batch']})")

    allowlist = set(CONFIG["channels"]["allowlist"] or [])
    received_at = now_iso()
    inserted = updated = duplicate = ignored = 0

    conn = db.connect(CONFIG["db"]["path"])
    try:
        with conn:
            for msg in batch.messages:
                if allowlist and msg.channelId not in allowlist:
                    ignored += 1
                    continue
                msg = msg.clamped()
                row = {
                    "message_id": msg.messageId,
                    "channel_id": msg.channelId,
                    "author": msg.author,
                    "content": msg.content,
                    "embeds": json.dumps(msg.embeds),
                    "links": json.dumps(msg.links),
                    "reply_parent_id": msg.reply.parentId if msg.reply else None,
                    "reply_preview": msg.reply.preview if msg.reply else None,
                    "ts": msg.timestamp,
                    "ts_approx": int(msg.timestampApprox),
                    "captured_at": msg.capturedAt,
                    "received_at": received_at,
                }
                cur = conn.execute(
                    """INSERT OR IGNORE INTO messages
                       (message_id, channel_id, author, content, embeds, links,
                        reply_parent_id, reply_preview, ts, ts_approx,
                        captured_at, received_at)
                       VALUES (:message_id, :channel_id, :author, :content,
                               :embeds, :links, :reply_parent_id, :reply_preview,
                               :ts, :ts_approx, :captured_at, :received_at)""",
                    row,
                )
                if cur.rowcount:
                    inserted += 1
                    continue
                # Existing id: update only if the content actually changed
                # (edits, late-loading embeds). Reset classification so the
                # new content gets reclassified.
                cur = conn.execute(
                    """UPDATE messages
                       SET content = :content, embeds = :embeds, links = :links,
                           edited = 1, received_at = :received_at,
                           tier = NULL, tier_reason = NULL, classified_at = NULL
                       WHERE message_id = :message_id
                         AND (content IS NOT :content
                              OR embeds IS NOT :embeds
                              OR links IS NOT :links)""",
                    row,
                )
                if cur.rowcount:
                    updated += 1
                else:
                    duplicate += 1
    finally:
        conn.close()

    log.info(
        "batch: %d in -> %d new, %d updated, %d dup, %d ignored",
        len(batch.messages), inserted, updated, duplicate, ignored,
    )
    return {
        "ok": True,
        "inserted": inserted,
        "updated": updated,
        "duplicate": duplicate,
        "ignored": ignored,
    }


@app.get("/health")
def health() -> dict:
    conn = db.connect(CONFIG["db"]["path"])
    try:
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        last = conn.execute("SELECT MAX(received_at) FROM messages").fetchone()[0]
    finally:
        conn.close()
    return {"ok": True, "messages": total, "last_received_at": last}
