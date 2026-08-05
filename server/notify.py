"""Alert generation + delivery queue.

Alerts are rows in alerts_sent with a dedupe_key (each event fires once) and a
JSON payload the extension renders as a desktop notification. The extension
polls GET /alerts?after=<id>; rate limiting happens at generation time.

Kinds: tier1 | consensus | watchdog | digest-ready
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import classify

BASE_DIR = Path(__file__).resolve().parent
CONFIG = yaml.safe_load((BASE_DIR / "config.yaml").read_text())
_A = CONFIG.get("alerts", {})
_W = CONFIG.get("watchdog", {})

log = logging.getLogger("notify")


def _now():
    return datetime.now(timezone.utc)


def _insert(conn, kind: str, dedupe_key: str, payload: dict,
            token_key: str | None = None, message_id: str | None = None) -> bool:
    with conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO alerts_sent
               (kind, token_key, message_id, dedupe_key, sent_at, payload)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (kind, token_key, message_id, dedupe_key,
             _now().isoformat(timespec="seconds"), json.dumps(payload)),
        )
    return bool(cur.rowcount)


def _recent_alert_count(conn, hours: float = 1.0) -> int:
    since = (_now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    return conn.execute(
        "SELECT COUNT(*) FROM alerts_sent WHERE sent_at >= ? AND kind != 'watchdog'",
        (since,),
    ).fetchone()[0]


def _token_snapshot(conn, message_id: str) -> tuple[str | None, dict]:
    row = conn.execute(
        """SELECT COALESCE(t.alias_of, mn.token_key) AS key, t2.enrichment
           FROM mentions mn
           LEFT JOIN tokens t ON t.token_key = mn.token_key
           LEFT JOIN tokens t2 ON t2.token_key = COALESCE(t.alias_of, mn.token_key)
           WHERE mn.message_id = ? AND mn.kind = 'contract' LIMIT 1""",
        (message_id,),
    ).fetchone()
    if not row:
        return None, {}
    return row["key"], json.loads(row["enrichment"] or "{}")


def _snapshot_line(s: dict) -> str:
    if s.get("status") != "ok":
        return "⚠ no on-chain data (pre-liquidity or dead)"
    bits = []
    if s.get("market_cap"):
        bits.append(f"MC ${s['market_cap']:,.0f}")
    if s.get("liquidity_usd") is not None:
        bits.append(f"Liq ${s['liquidity_usd']:,.0f}")
    if s.get("age_hours") is not None:
        bits.append(f"Age {s['age_hours']:.0f}h")
    if s.get("rug_score") is not None:
        bits.append(f"Rug {s['rug_score']}/100")
    line = " · ".join(bits)
    flags = s.get("flags") or []
    if flags:
        line += "\n⚠ " + " · ".join(flags)
    return line


def generate_alerts(conn) -> int:
    """Scan for new alert-worthy events. Returns number of alerts created."""
    created = 0
    hourly_cap = _A.get("max_per_hour", 20)

    # --- tier 1 messages ---
    rows = conn.execute(
        """SELECT m.message_id, m.author, m.content, m.tier_reason
           FROM messages m
           LEFT JOIN alerts_sent a ON a.dedupe_key = 'tier1:' || m.message_id
           WHERE m.tier = 1 AND a.id IS NULL
           ORDER BY m.message_id DESC LIMIT 50"""
    ).fetchall()
    for r in rows:
        if _recent_alert_count(conn) >= hourly_cap:
            log.warning("alert rate cap reached (%d/h) — suppressing", hourly_cap)
            break
        token_key, snap = _token_snapshot(conn, r["message_id"])
        symbol = snap.get("symbol")
        title = f"Signal · {('$' + symbol) if symbol else (r['tier_reason'] or 'tier 1')}"
        body = f"“{(r['content'] or '')[:140]}” — {r['author']}"
        if snap:
            body += "\n" + _snapshot_line(snap)
        payload = {"title": title, "body": body, "url": snap.get("url")}
        if _insert(conn, "tier1", f"tier1:{r['message_id']}", payload,
                   token_key, r["message_id"]):
            created += 1

    # --- consensus events ---
    for ev in classify.find_consensus(conn, since_hours=6):
        if not ev["consensus"]:
            continue
        bucket = (ev["consensus_start"] or "")[:13]  # hour bucket
        key = f"consensus:{ev['token_key']}:{bucket}"
        trow = conn.execute(
            "SELECT ticker, enrichment FROM tokens WHERE token_key = ?",
            (ev["token_key"],),
        ).fetchone()
        snap = json.loads(trow["enrichment"] or "{}") if trow else {}
        label = ("$" + trow["ticker"]) if trow and trow["ticker"] else ev["token_key"]
        payload = {
            "title": f"Consensus · {label}",
            "body": (f"{ev['consensus_authors']} authors / "
                     f"{ev['consensus_mentions']} mentions in "
                     f"{CONFIG['classify']['consensus']['window_minutes']}min\n"
                     + _snapshot_line(snap)),
            "url": snap.get("url"),
        }
        if _insert(conn, "consensus", key, payload, ev["token_key"]):
            created += 1

    return created


def watchdog_check(conn) -> bool:
    """Alert when ingestion goes silent during hours the channel is active."""
    quiet_min = _W.get("quiet_minutes", 60)
    h0, h1 = _W.get("active_hours_utc", [0, 24])
    now = _now()
    if not (h0 <= now.hour < h1):
        return False
    last = conn.execute("SELECT MAX(received_at) FROM messages").fetchone()[0]
    if not last:
        return False
    last_dt = datetime.fromisoformat(last)
    if now - last_dt < timedelta(minutes=quiet_min):
        return False
    payload = {
        "title": "⚠ Capture silent",
        "body": (f"No messages received for {int((now - last_dt).total_seconds() // 60)} min "
                 f"(last: {last[:16]} UTC). Check the Discord tab / extension / selectors."),
        "url": None,
    }
    return _insert(conn, "watchdog", f"watchdog:{last}", payload)


def digest_ready_check(conn, digest_url: str | None) -> bool:
    """Once per day at the configured hour, nudge with a link to the digest page."""
    hour = _A.get("digest_hour_utc", 8)
    now = _now()
    if now.hour < hour:
        return False
    day = now.date().isoformat()
    n = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE COALESCE(ts, captured_at) >= ?",
        ((now - timedelta(hours=24)).isoformat(timespec="seconds"),),
    ).fetchone()[0]
    if n == 0:
        return False
    payload = {
        "title": f"Daily digest ready — {day}",
        "body": f"{n} messages in the last 24h. Open the digest for the ranked view.",
        "url": digest_url,
    }
    return _insert(conn, "digest-ready", f"digest:{day}", payload)


def alerts_after(conn, after_id: int, limit: int | None = None) -> list[dict]:
    limit = limit or _A.get("max_per_poll", 5)
    rows = conn.execute(
        "SELECT id, kind, payload, sent_at FROM alerts_sent WHERE id > ? ORDER BY id LIMIT ?",
        (after_id, limit),
    ).fetchall()
    return [
        {"id": r["id"], "kind": r["kind"], "sent_at": r["sent_at"],
         **json.loads(r["payload"] or "{}")}
        for r in rows
    ]
