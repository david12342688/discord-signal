"""Entity extraction: contract addresses, $TICKERS, and token-bearing URLs.

Pure functions, no I/O — easy to test against real captured messages.
"""

import re
from datetime import datetime, timezone
from urllib.parse import urlparse

# Discord epoch (ms) — snowflake IDs encode their creation time.
DISCORD_EPOCH_MS = 1420070400000

EVM_RE = re.compile(r"\b(0x[0-9a-fA-F]{40})\b")
# Base58, 32-44 chars, excludes 0 O I l. Guarded so we don't match inside
# longer alphanumeric runs (tx hashes, URLs handled separately).
SOLANA_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z0OIl/])([1-9A-HJ-NP-Za-km-z]{32,44})(?![1-9A-HJ-NP-Za-km-z])")
TICKER_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,14})\b")
URL_RE = re.compile(r"https?://\S+")

# Common words that look like Solana base58 but aren't (rare at 32+ chars,
# but keep the hook for false positives discovered in the wild).
SOLANA_BLOCKLIST: set[str] = set()

# Ticker-looking strings that are noise, not tokens.
TICKER_BLOCKLIST = {"USD", "USDT", "USDC", "SOL", "ETH", "BTC", "BNB", "K", "M", "B"}


def snowflake_to_iso(message_id: str) -> str:
    """Derive the real message timestamp from a Discord snowflake."""
    ms = (int(message_id) >> 22) + DISCORD_EPOCH_MS
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def extract_from_url(url: str) -> list[dict]:
    """Pull token addresses out of known URL shapes (dexscreener, pump.fun,
    birdeye, solscan, gmgn...)."""
    out = []
    try:
        parsed = urlparse(url)
    except ValueError:
        return out
    host = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.split("/") if p]

    def classify(seg: str):
        if EVM_RE.fullmatch(seg):
            return {"kind": "contract", "chain_hint": "evm", "address": seg, "raw": url}
        if re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", seg):
            return {"kind": "contract", "chain_hint": "solana", "address": seg, "raw": url}
        return None

    if any(h in host for h in ("dexscreener.com", "pump.fun", "birdeye.so",
                               "solscan.io", "gmgn.ai", "dextools.io",
                               "geckoterminal.com", "photon-sol.tinyastro.io")):
        for seg in parts:
            hit = classify(seg)
            if hit:
                out.append(hit)
    return out


def extract_entities(content: str, links: list[str], embeds: list[str]) -> list[dict]:
    """Return a list of entity dicts: {kind, raw, ...}.

    kinds: contract (with chain_hint + address), ticker (with symbol), url.
    """
    entities = []
    seen = set()

    def add(e: dict):
        key = (e["kind"], e.get("address") or e.get("symbol") or e.get("raw"))
        if key not in seen:
            seen.add(key)
            entities.append(e)

    text_blobs = [content] + embeds

    for blob in text_blobs:
        # Strip URLs from the blob before base58 matching so URL fragments
        # don't produce phantom mints; URLs get their own targeted parser.
        urls_in_blob = URL_RE.findall(blob)
        stripped = URL_RE.sub(" ", blob)

        for m in EVM_RE.finditer(stripped):
            add({"kind": "contract", "chain_hint": "evm", "address": m.group(1), "raw": m.group(1)})
        for m in SOLANA_RE.finditer(stripped):
            candidate = m.group(1)
            if candidate in SOLANA_BLOCKLIST:
                continue
            add({"kind": "contract", "chain_hint": "solana", "address": candidate, "raw": candidate})
        for m in TICKER_RE.finditer(stripped):
            symbol = m.group(1).upper()
            if symbol in TICKER_BLOCKLIST or symbol.isdigit():
                continue
            add({"kind": "ticker", "symbol": symbol, "raw": m.group(0)})

        for url in urls_in_blob:
            for hit in extract_from_url(url):
                add(hit)
            add({"kind": "url", "raw": url.rstrip('.,)>]')})

    for url in links:
        for hit in extract_from_url(url):
            add(hit)
        add({"kind": "url", "raw": url})

    return entities


def token_key_for(entity: dict, resolved_chain: str | None = None) -> str | None:
    """Canonical tokens.token_key for an entity, or None for plain URLs."""
    if entity["kind"] == "contract":
        chain = resolved_chain or entity.get("chain_hint") or "unknown"
        addr = entity["address"]
        if chain != "solana":
            addr = addr.lower()
        return f"{chain}:{addr}"
    if entity["kind"] == "ticker":
        return f"ticker:${entity['symbol']}"
    return None
