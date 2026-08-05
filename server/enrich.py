"""On-chain enrichment: Dexscreener, RugCheck, GeckoTerminal.

Caching lives in the tokens table (enrichment JSON + last_enriched_at).
Rate limiting is a simple per-host minimum interval; failures degrade
gracefully — callers always get a dict, possibly {"status": "unavailable"}.
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml

BASE_DIR = Path(__file__).resolve().parent
CONFIG = yaml.safe_load((BASE_DIR / "config.yaml").read_text())
_E = CONFIG.get("enrich", {})

log = logging.getLogger("enrich")

# min seconds between requests per host (config-tunable)
_HOST_INTERVALS = {
    "api.dexscreener.com": _E.get("dexscreener_interval_s", 0.3),
    "api.rugcheck.xyz": _E.get("rugcheck_interval_s", 2.0),
    "api.geckoterminal.com": _E.get("geckoterminal_interval_s", 2.5),
}
_last_call: dict[str, float] = {}

CACHE_TTL_S = _E.get("cache_ttl_s", 300)
EVM_CHAINS = _E.get("evm_chains", ["bsc", "base", "ethereum"])


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _get(url: str, timeout: float = 10.0):
    host = httpx.URL(url).host
    wait = _HOST_INTERVALS.get(host, 1.0) - (time.monotonic() - _last_call.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last_call[host] = time.monotonic()
    for attempt in range(3):
        try:
            r = httpx.get(url, timeout=timeout, headers={"User-Agent": "discord-signal/0.1"})
            if r.status_code == 429:
                time.sleep(2 ** attempt * 2)
                continue
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            log.warning("GET %s failed (attempt %d): %s", url, attempt + 1, e)
            time.sleep(1 + attempt)
    return None


def dexscreener_lookup(chain_hint: str, address: str) -> dict | None:
    """Find the token's best pair. For EVM addresses the chain is unknown —
    probe the configured chains in order. Returns {chain, pair} or None."""
    chains = [chain_hint] if chain_hint == "solana" else EVM_CHAINS
    for chain in chains:
        data = _get(f"https://api.dexscreener.com/tokens/v1/{chain}/{address}")
        if data:
            best = max(data, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
            return {"chain": chain, "pair": best}
    return None


def rugcheck_summary(mint: str) -> dict | None:
    return _get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary")


def summarize(dex: dict | None, rug: dict | None) -> dict:
    """Flatten API responses into the compact snapshot alerts/digests use."""
    if not dex:
        return {"status": "pre-liquidity", "fetched_at": now_iso()}
    p = dex["pair"]
    liq = (p.get("liquidity") or {}).get("usd")
    created = p.get("pairCreatedAt")
    age_h = round((time.time() * 1000 - created) / 3600000, 1) if created else None
    out = {
        "status": "ok",
        "fetched_at": now_iso(),
        "chain": dex["chain"],
        "dex": p.get("dexId"),
        "name": (p.get("baseToken") or {}).get("name"),
        "symbol": (p.get("baseToken") or {}).get("symbol"),
        "address": (p.get("baseToken") or {}).get("address"),
        "price_usd": p.get("priceUsd"),
        "liquidity_usd": liq,
        "market_cap": p.get("marketCap") or p.get("fdv"),
        "volume_h24": (p.get("volume") or {}).get("h24"),
        "price_change_h1": (p.get("priceChange") or {}).get("h1"),
        "price_change_h24": (p.get("priceChange") or {}).get("h24"),
        "txns_h1": (p.get("txns") or {}).get("h1"),
        "age_hours": age_h,
        "url": p.get("url"),
    }
    if rug:
        out["rug_score"] = rug.get("score_normalised")  # 0-100, higher = riskier
        out["lp_locked_pct"] = rug.get("lpLockedPct")
        out["rug_risks"] = [
            {"name": r.get("name"), "level": r.get("level")}
            for r in (rug.get("risks") or [])
        ]
    return out


def flags_for(snapshot: dict) -> list[str]:
    """Auto-skepticism: human-readable red flags derived from the snapshot."""
    flags = []
    if snapshot.get("status") == "pre-liquidity":
        return ["no Dexscreener presence yet (pre-liquidity or dead)"]
    t = _E.get("flags", {})
    liq = snapshot.get("liquidity_usd")
    if liq is not None and liq < t.get("min_liquidity_usd", 10000):
        flags.append(f"LOW LIQUIDITY ${liq:,.0f}")
    age = snapshot.get("age_hours")
    if age is not None and age < t.get("min_age_hours", 24):
        flags.append(f"NEW TOKEN — {age:.1f}h old")
    rug = snapshot.get("rug_score")
    if rug is not None and rug >= t.get("max_rug_score", 50):
        flags.append(f"RUG SCORE {rug}/100")
    lp = snapshot.get("lp_locked_pct")
    if lp is not None and lp < t.get("min_lp_locked_pct", 50):
        flags.append(f"LP ONLY {lp:.0f}% LOCKED")
    chg = snapshot.get("price_change_h1")
    if chg is not None and chg <= -50:
        flags.append(f"PRICE {chg:+.0f}% in 1h")
    return flags


def enrich_token(conn, token_key: str, chain_hint: str, address: str | None,
                 ticker: str | None) -> dict:
    """Enrich with cache-through on the tokens table. Returns the snapshot."""
    row = conn.execute(
        "SELECT enrichment, last_enriched_at FROM tokens WHERE token_key = ?",
        (token_key,),
    ).fetchone()
    if row and row["enrichment"] and row["last_enriched_at"]:
        age = time.time() - datetime.fromisoformat(row["last_enriched_at"]).timestamp()
        if age < CACHE_TTL_S:
            return json.loads(row["enrichment"])

    if not address:
        # Bare ticker with no contract — nothing to look up on-chain yet.
        snapshot = {"status": "unresolved-ticker", "fetched_at": now_iso()}
    else:
        dex = dexscreener_lookup(chain_hint, address)
        rug = None
        if dex and dex["chain"] == "solana":
            rug = rugcheck_summary(address)
        snapshot = summarize(dex, rug)
        snapshot["flags"] = flags_for(snapshot)

    with conn:
        conn.execute(
            """INSERT INTO tokens (token_key, chain, address, ticker, name,
                                   first_seen_at, last_enriched_at, enrichment)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(token_key) DO UPDATE SET
                 chain = COALESCE(excluded.chain, tokens.chain),
                 name = COALESCE(excluded.name, tokens.name),
                 ticker = COALESCE(excluded.ticker, tokens.ticker),
                 last_enriched_at = excluded.last_enriched_at,
                 enrichment = excluded.enrichment""",
            (
                token_key,
                snapshot.get("chain"),
                address,
                ticker or snapshot.get("symbol"),
                snapshot.get("name"),
                now_iso(),
                now_iso(),
                json.dumps(snapshot),
            ),
        )
    return snapshot
