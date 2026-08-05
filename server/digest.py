"""Digest builder: assembles the ranked 24h view (consensus, alerts, warnings,
meta, filtered ratio) as structured data + rendered markdown."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import classify
import db

BASE_DIR = Path(__file__).resolve().parent
CONFIG = yaml.safe_load((BASE_DIR / "config.yaml").read_text())


def _fmt_usd(v):
    if v is None:
        return "?"
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v / 1_000:.0f}k"
    return f"${v:,.0f}"


def _token_line(snapshot: dict) -> str:
    if snapshot.get("status") != "ok":
        return snapshot.get("status", "enrichment unavailable")
    parts = [
        f"MC {_fmt_usd(snapshot.get('market_cap'))}",
        f"Liq {_fmt_usd(snapshot.get('liquidity_usd'))}",
    ]
    if snapshot.get("age_hours") is not None:
        age = snapshot["age_hours"]
        parts.append(f"Age {age:.0f}h" if age < 48 else f"Age {age / 24:.0f}d")
    if snapshot.get("lp_locked_pct") is not None:
        parts.append(f"LP locked {snapshot['lp_locked_pct']:.0f}%")
    if snapshot.get("rug_score") is not None:
        parts.append(f"Rug {snapshot['rug_score']}/100")
    if snapshot.get("price_change_h24") is not None:
        parts.append(f"{snapshot['price_change_h24']:+.0f}% 24h")
    return " · ".join(parts)


def build_digest(conn, hours: float = 24.0) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")

    totals = dict(conn.execute(
        """SELECT COUNT(*) AS total,
                  SUM(tier = 1) AS t1, SUM(tier = 2) AS t2, SUM(tier = 3) AS t3
           FROM messages WHERE COALESCE(ts, captured_at) >= ?""",
        (since,),
    ).fetchone())

    def msg_rows(tier):
        return [dict(r) for r in conn.execute(
            """SELECT message_id, author, content, tier_reason, ts, captured_at
               FROM messages WHERE tier = ? AND COALESCE(ts, captured_at) >= ?
               ORDER BY message_id""",
            (tier, since),
        )]

    tokens = {}
    for r in conn.execute("SELECT token_key, ticker, name, enrichment FROM tokens"):
        tokens[r["token_key"]] = {
            "ticker": r["ticker"], "name": r["name"],
            "snapshot": json.loads(r["enrichment"]) if r["enrichment"] else {},
        }

    consensus = classify.find_consensus(conn, since_hours=hours)

    # attach sample quotes per token
    for ev in consensus:
        rows = conn.execute(
            """SELECT DISTINCT msg.author, msg.content
               FROM mentions mn JOIN messages msg ON msg.message_id = mn.message_id
               LEFT JOIN tokens t ON t.token_key = mn.token_key
               WHERE COALESCE(t.alias_of, mn.token_key) = ? AND msg.content != ''
               ORDER BY LENGTH(msg.content) DESC LIMIT 2""",
            (ev["token_key"],),
        ).fetchall()
        ev["quotes"] = [dict(r) for r in rows]
        ev["token"] = tokens.get(ev["token_key"], {})

    warnings = [m for m in msg_rows(1) if "warning" in (m["tier_reason"] or "")]
    time_sensitive = [m for m in msg_rows(1) if "time-sensitive" in (m["tier_reason"] or "")]
    tier1_other = [m for m in msg_rows(1) if m not in warnings and m not in time_sensitive]
    meta = msg_rows(2)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "since": since,
        "totals": totals,
        "consensus": consensus,
        "warnings": warnings,
        "time_sensitive": time_sensitive,
        "tier1": tier1_other,
        "meta": meta,
    }


def render_markdown(d: dict) -> str:
    day = d["generated_at"][:10]
    lines = [f"## 24h Digest — {day}", ""]

    hot = [e for e in d["consensus"] if e["consensus"]]
    if hot:
        lines.append("### Consensus forming")
        for ev in hot:
            tok = ev["token"]
            label = tok.get("ticker") and f"${tok['ticker']}" or ev["token_key"]
            window = ev["consensus_start"][11:16] if ev["consensus_start"] else "?"
            lines.append(
                f"{label}  — {ev['mentions']} mentions / {ev['authors']} authors, "
                f"clustered from {window} ({ev['consensus_authors']} authors in "
                f"{CONFIG['classify']['consensus']['window_minutes']}min)"
            )
            snap = tok.get("snapshot", {})
            lines.append(f"       {_token_line(snap)}")
            for f in snap.get("flags", []):
                lines.append(f"       ⚠ {f}")
            if snap.get("url"):
                lines.append(f"       {snap['url']}")
            for q in ev["quotes"][:1]:
                lines.append(f'       "{q["content"][:120]}" — {q["author"]}')
            lines.append("")

    if d["time_sensitive"]:
        lines.append("### Time-sensitive")
        for m in d["time_sensitive"]:
            lines.append(f'- "{m["content"][:140]}" — {m["author"]}')
        lines.append("")

    if d["warnings"]:
        lines.append("### Warnings")
        for m in d["warnings"]:
            lines.append(f'- "{m["content"][:140]}" — {m["author"]}')
        lines.append("")

    if d["tier1"]:
        lines.append("### Signals")
        for m in d["tier1"]:
            lines.append(f'- "{m["content"][:140]}" — {m["author"]} ({m["tier_reason"]})')
        lines.append("")

    others = [e for e in d["consensus"] if not e["consensus"] and e["mentions"] > 1]
    if others:
        lines.append("### Also mentioned")
        for ev in others[:10]:
            tok = ev["token"]
            label = tok.get("ticker") and f"${tok['ticker']}" or ev["token_key"]
            lines.append(f"- {label}: {ev['mentions']} mentions / {ev['authors']} authors — "
                         f"{_token_line(tok.get('snapshot', {}))}")
        lines.append("")

    if d["meta"]:
        lines.append("### Digest (tier 2)")
        for m in d["meta"][:15]:
            lines.append(f'- "{m["content"][:120]}" — {m["author"]} ({m["tier_reason"]})')
        lines.append("")

    t = d["totals"]
    surfaced = (t["t1"] or 0) + (t["t2"] or 0)
    lines.append("### Filtered")
    lines.append(f"{t['total']} messages · {surfaced} surfaced · {t['t3'] or 0} dropped")
    return "\n".join(lines)


if __name__ == "__main__":
    conn = db.connect(CONFIG["db"]["path"])
    d = build_digest(conn)
    print(render_markdown(d))
