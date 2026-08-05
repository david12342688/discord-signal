"""Ingest endpoint + pipeline host.

POST /ingest   shared-secret auth -> validate -> dedupe on message id -> SQLite
GET  /alerts   shared-secret auth -> alerts after ?after=<id> (extension polls)
GET  /digest   digest page, ?key=<secret> (browser view)
GET  /health   liveness + basic counters (no auth, exposes no message content)

A background thread runs the classify/enrich/alert loop every
pipeline.tick_seconds.

Run:  .venv/bin/uvicorn ingest:app --host 0.0.0.0 --port 8787
"""

import hmac
import html
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import yaml
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
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


# --- background pipeline loop -------------------------------------------------

def _pipeline_loop(stop: threading.Event):
    import classify
    import notify

    tick = CONFIG.get("pipeline", {}).get("tick_seconds", 30)
    while not stop.is_set():
        try:
            conn = db.connect(CONFIG["db"]["path"])
            try:
                stats = classify.process_pending(conn)
                if stats["processed"]:
                    log.info("pipeline: %s", stats)
                n = notify.generate_alerts(conn)
                if n:
                    log.info("pipeline: %d new alerts", n)
                notify.watchdog_check(conn)
                public = CONFIG["server"].get("public_url", "").rstrip("/")
                digest_url = f"{public}/digest?key={INGEST_SECRET}" if public else None
                notify.digest_ready_check(conn, digest_url)
            finally:
                conn.close()
        except Exception:
            log.exception("pipeline tick failed")
        stop.wait(tick)


@asynccontextmanager
async def lifespan(app):
    stop = threading.Event()
    t = threading.Thread(target=_pipeline_loop, args=(stop,), daemon=True, name="pipeline")
    t.start()
    log.info("pipeline loop started")
    yield
    stop.set()


# --- app ---------------------------------------------------------------------

app = FastAPI(title="discord-signal ingest", lifespan=lifespan)


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


@app.get("/alerts", dependencies=[Depends(require_auth)])
def alerts(after: int = 0) -> dict:
    import notify

    conn = db.connect(CONFIG["db"]["path"])
    try:
        items = notify.alerts_after(conn, after)
    finally:
        conn.close()
    return {"ok": True, "alerts": items}


@app.get("/digest", response_class=HTMLResponse)
def digest_page(key: Annotated[str, Query()] = "") -> str:
    if not INGEST_SECRET or not hmac.compare_digest(key, INGEST_SECRET):
        raise HTTPException(401, "bad key")
    import digest as digest_mod

    conn = db.connect(CONFIG["db"]["path"])
    try:
        md = digest_mod.render_markdown(digest_mod.build_digest(conn))
    finally:
        conn.close()
    body = html.escape(md)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Signal Digest</title>
<style>
 body {{ background:#15171C; color:#E6E3DA; font:14px/1.6 ui-monospace,Menlo,Consolas,monospace;
        max-width: 900px; margin: 0 auto; padding: 32px 16px; }}
 pre {{ white-space: pre-wrap; word-break: break-word; }}
 a {{ color:#E8A62B; }}
</style></head><body><pre>{body}</pre></body></html>"""


@app.get("/health")
def health() -> dict:
    conn = db.connect(CONFIG["db"]["path"])
    try:
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        last = conn.execute("SELECT MAX(received_at) FROM messages").fetchone()[0]
    finally:
        conn.close()
    return {"ok": True, "messages": total, "last_received_at": last}
