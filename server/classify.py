"""Classification: heuristic pre-filter -> LLM triage for the ambiguous
remainder -> consensus/velocity aggregation.

Tiers: 1 = push alert, 2 = digest only, 3 = log silently.
"""

import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

import db
import enrich
import extract

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
CONFIG = yaml.safe_load((BASE_DIR / "config.yaml").read_text())
_C = CONFIG.get("classify", {})

log = logging.getLogger("classify")

# --- heuristics --------------------------------------------------------------

NOISE_EXACT = {
    "gm", "gn", "lfg", "lfgg", "lfggg", "wagmi", "ngmi", "ser", "wen", "kek",
    "lol", "lmao", "lmfao", "based", "cope", "hopium", "rip", "f", "oof",
    "gg", "ok", "okay", "yes", "no", "yeah", "nah", "true", "real", "fr",
    "same", "this", "wild", "crazy", "insane", "damn", "bruh", "sheesh",
}
NOISE_PHRASES = [
    r"^we('| a)re so back", r"^it'?s over$", r"^i'?m (so )?(cooked|done|dead)",
    r"^trust( me)?\.?$", r"^soon\.?$", r"^inshallah", r"^valhalla",
]
WARNING_WORDS = re.compile(
    r"\b(rug(ged|pull)?|honeypot|scam(mer)?|dev\s+(sold|selling|dump)|"
    r"lp\s+(pulled|removed|unlocked)|drained|exploit(ed)?|hacked|"
    r"can'?t sell|cant sell|freeze|blacklist)\b", re.I)
CALL_WORDS = re.compile(
    r"\b(ape(d|ing)?|bid(ding)?|buy(ing)?|bought|long(ed)?|short(ed)?|"
    r"entry|entered|sold|selling|sell|took profit|tp'?d|full port|"
    r"accumulat(e|ing|ed)|loaded|load(ing)? up|sent it|degen'?d)\b", re.I)
# Time-sensitive needs BOTH an event word and a time phrase — a bare
# "in 10 mins" is commentary, not an event (tuning fix 2026-08-05).
TIME_EVENT = re.compile(
    r"\b(launch(es|ing)?|presale|snapshot|airdrop|listing|list(s|ed)?\s+on|"
    r"deploy(s|ing|ed)?|mint(ing)?|tge|ico|cex)\b", re.I)
TIME_PHRASE = re.compile(
    r"\b(in \d+ ?(min|minute|hour|h)s?|at \d{1,2}(:\d{2})? ?(utc|am|pm|est|cet)?|"
    r"today|tonight|tomorrow|soon|now live|is live)\b", re.I)
META_WORDS = re.compile(
    r"\b(meta|narrative|rotat(e|ing|ion)|szn|season)\b", re.I)


def heuristic_tier(msg: dict, entities: list[dict]) -> tuple[int | None, str]:
    """Return (tier, reason) or (None, '') when ambiguous -> LLM decides."""
    text = (msg["content"] or "").strip()
    lower = text.lower()
    has_contract = any(e["kind"] == "contract" for e in entities)
    has_ticker = any(e["kind"] == "ticker" for e in entities)

    if WARNING_WORDS.search(text):
        return 1, "warning language (rug/scam/dev activity)"
    if has_contract:
        return 1, "contract address posted"
    if TIME_EVENT.search(text) and TIME_PHRASE.search(text):
        return 1, "time-sensitive (launch/presale/listing/snapshot)"
    if (has_ticker or has_contract) and CALL_WORDS.search(text):
        return 1, "entry/exit call on a named token"

    # Obvious noise
    if not text and not entities:
        return 3, "empty after extraction"
    if len(text) <= _C.get("noise_max_len", 12) and not entities:
        if lower in NOISE_EXACT or not re.search(r"[a-zA-Z]{3}", text):
            return 3, "one-word/emoji noise"
    if lower in NOISE_EXACT:
        return 3, "stock degen phrase"
    for pat in NOISE_PHRASES:
        if re.search(pat, lower):
            return 3, "hopium/cope phrase"
    if len(text) < _C.get("short_banter_max_len", 25) and not entities \
            and not META_WORDS.search(text):
        return 3, "short banter, no entities"

    if META_WORDS.search(text):
        return 2, "meta/narrative discussion"
    if has_ticker:
        return 2, "token discussion without a hard call"

    return None, ""  # ambiguous -> LLM


# --- LLM triage (OpenRouter) --------------------------------------------------

TRIAGE_SYSTEM = """You triage messages from a high-noise memecoin trading Discord channel.
Assign each message a tier:

TIER 1 (push alert): actionable trading signal - conviction entry/exit calls, credible
rug/scam warnings, time-sensitive events (launches, snapshots, listings), whale movement
reports, contract shares with context.
TIER 2 (digest only): meta/narrative shifts, sustained token discussion without a hard
call, market structure observations, useful links or research.
TIER 3 (drop): banter, hopium, cope, portfolio crying, price commentary with nothing
actionable, in-jokes, arguments, reaction chatter.

Watch for sarcasm: "this is definitely not a rug" usually means it IS suspicious - that
is tier 1 warning material. Reply context (field "replying_to") may change meaning.
Set confident=false only when the message genuinely could be tier 1 but you cannot tell.

Reply ONLY with a JSON object, no other text:
{"results": [{"message_id": "<id>", "tier": <1|2|3>, "reason": "<short>", "confident": <bool>}]}"""


def _openrouter_chat(model: str, payload: list[dict]) -> list[dict]:
    import httpx

    key = os.environ.get("OPENROUTER_API_KEY", "")
    resp = httpx.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": TRIAGE_SYSTEM},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        },
        timeout=120,
    )
    data = resp.json()
    if "choices" not in data:
        log.warning("openrouter %s error: %s", model, str(data)[:200])
        return []
    text = data["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        log.warning("openrouter %s unparseable output", model)
        return []
    try:
        return json.loads(m.group(0))["results"]
    except (json.JSONDecodeError, KeyError) as e:
        log.warning("openrouter %s bad json: %s", model, e)
        return []


def llm_triage(messages: list[dict]) -> dict[str, tuple[int, str]]:
    """Classify ambiguous messages in batches. Returns {message_id: (tier, reason)}.
    Empty dict if no API key is configured (caller applies the fallback)."""
    if not messages or not os.environ.get("OPENROUTER_API_KEY"):
        return {}

    cfg = _C.get("llm", {})
    triage_model = cfg.get("triage_model", "google/gemini-2.5-flash-lite")
    escalation_model = cfg.get("escalation_model", "deepseek/deepseek-v4-flash-0731")
    batch_size = cfg.get("batch_size", 30)

    out: dict[str, tuple[int, str]] = {}
    unsure: list[dict] = []

    def run(model: str, batch: list[dict]) -> list[dict]:
        payload = [
            {
                "message_id": m["message_id"],
                "author": m["author"],
                "text": m["content"],
                **({"replying_to": m["reply_preview"]} if m.get("reply_preview") else {}),
            }
            for m in batch
        ]
        return _openrouter_chat(model, payload)

    for i in range(0, len(messages), batch_size):
        batch = messages[i : i + batch_size]
        by_id = {m["message_id"]: m for m in batch}
        for r in run(triage_model, batch):
            mid, tier = str(r.get("message_id")), r.get("tier")
            if mid not in by_id or tier not in (1, 2, 3):
                continue
            if r.get("confident", True):
                out[mid] = (tier, f"llm: {r.get('reason', '')}")
            else:
                unsure.append(by_id[mid])

    # Genuinely ambiguous cases escalate to the stronger model.
    for i in range(0, len(unsure), batch_size):
        for r in run(escalation_model, unsure[i : i + batch_size]):
            mid, tier = str(r.get("message_id")), r.get("tier")
            if tier in (1, 2, 3):
                out[mid] = (tier, f"llm+: {r.get('reason', '')}")

    return out


# --- pipeline ----------------------------------------------------------------

def process_pending(conn) -> dict:
    """Extract entities, enrich, classify everything with classified_at IS NULL."""
    rows = conn.execute(
        """SELECT message_id, channel_id, author, content, embeds, links,
                  reply_preview, ts, ts_approx, captured_at
           FROM messages WHERE classified_at IS NULL
           ORDER BY message_id"""
    ).fetchall()

    stats = {"processed": 0, "tier1": 0, "tier2": 0, "tier3": 0,
             "heuristic": 0, "llm": 0, "fallback": 0, "mentions": 0}
    ambiguous: list[dict] = []
    decided: dict[str, tuple[int, str]] = {}

    # Tuning fix: someone who just posted a contract gets context — their next
    # short messages ("i am in this") aren't banter.
    post_ca_min = _C.get("context", {}).get("post_ca_minutes", 10)
    last_ca_by_author: dict[str, datetime] = {}
    for r in conn.execute(
        """SELECT author, MAX(mentioned_at) AS t FROM mentions
           WHERE kind = 'contract' AND author IS NOT NULL GROUP BY author"""
    ):
        try:
            last_ca_by_author[r["author"]] = datetime.fromisoformat(r["t"])
        except (TypeError, ValueError):
            pass

    # Tuning fix: bare tickers (no $) match once the token is known in-channel.
    min_len = _C.get("known_ticker_min_len", 3)
    known_tickers = {
        r["ticker"].upper(): r["token_key"]
        for r in conn.execute(
            "SELECT ticker, COALESCE(alias_of, token_key) AS token_key FROM tokens "
            "WHERE ticker IS NOT NULL"
        )
        if len(r["ticker"] or "") >= min_len
    }

    for row in rows:
        msg = dict(row)
        entities = extract.extract_entities(
            msg["content"] or "", json.loads(msg["links"]), json.loads(msg["embeds"])
        )
        mentioned_at = msg["ts"] or extract.snowflake_to_iso(msg["message_id"])

        for word in re.findall(r"[A-Za-z]{%d,15}" % min_len, msg["content"] or ""):
            token_key = known_tickers.get(word.upper())
            if token_key and not any(
                e["kind"] == "ticker" and e.get("symbol") == word.upper() for e in entities
            ):
                with conn:
                    conn.execute(
                        """INSERT OR IGNORE INTO mentions
                           (message_id, token_key, kind, raw, author, mentioned_at)
                           VALUES (?, ?, 'ticker', ?, ?, ?)""",
                        (msg["message_id"], token_key, word, msg["author"], mentioned_at),
                    )
                    stats["mentions"] += 1
                entities.append({"kind": "ticker", "symbol": word.upper(), "raw": word,
                                 "known": True})

        # Record mentions + enrich contract tokens (ticker->contract aliasing
        # happens when enrichment reveals the symbol).
        contract_keys_by_symbol = {}
        for e in entities:
            if e.get("known"):
                continue  # mention already recorded under the canonical key
            key = extract.token_key_for(e)
            if not key:
                continue
            if e["kind"] == "contract":
                snapshot = enrich.enrich_token(
                    conn, key, e.get("chain_hint", "unknown"), e["address"], None
                )
                if snapshot.get("chain") and e.get("chain_hint") != snapshot["chain"]:
                    old_key, key = key, extract.token_key_for(e, resolved_chain=snapshot["chain"])
                    enrich.enrich_token(conn, key, snapshot["chain"], e["address"], None)
                    with conn:  # merge the unresolved placeholder into the real chain key
                        conn.execute(
                            "UPDATE tokens SET alias_of = ? WHERE token_key = ? AND alias_of IS NULL",
                            (key, old_key),
                        )
                if snapshot.get("symbol"):
                    contract_keys_by_symbol[snapshot["symbol"].upper()] = key
            with conn:
                conn.execute(
                    """INSERT OR IGNORE INTO mentions
                       (message_id, token_key, kind, raw, author, mentioned_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (msg["message_id"], key, e["kind"], e["raw"], msg["author"], mentioned_at),
                )
                stats["mentions"] += 1

        # Alias tickers to contracts seen in the same message
        for e in entities:
            if e["kind"] == "ticker" and e["symbol"] in contract_keys_by_symbol:
                with conn:
                    conn.execute(
                        "UPDATE tokens SET alias_of = ? WHERE token_key = ? AND alias_of IS NULL",
                        (contract_keys_by_symbol[e["symbol"]], f"ticker:${e['symbol']}"),
                    )

        tier, reason = heuristic_tier(msg, entities)

        # post-CA context bump: short banter from a recent contract-poster
        # is an entry/exit signal, not noise
        if tier == 3 and msg["author"] in last_ca_by_author:
            try:
                gap = datetime.fromisoformat(mentioned_at) - last_ca_by_author[msg["author"]]
                if timedelta(0) <= gap <= timedelta(minutes=post_ca_min):
                    tier, reason = 2, f"post-CA context ({msg['author']} shared a contract {int(gap.total_seconds() // 60)}min ago)"
            except (TypeError, ValueError):
                pass
        if any(e["kind"] == "contract" for e in entities):
            last_ca_by_author[msg["author"]] = datetime.fromisoformat(mentioned_at)

        if tier is not None:
            decided[msg["message_id"]] = (tier, reason)
            stats["heuristic"] += 1
        else:
            ambiguous.append(msg)

    llm_results = llm_triage(ambiguous)
    for msg in ambiguous:
        if msg["message_id"] in llm_results:
            decided[msg["message_id"]] = llm_results[msg["message_id"]]
            stats["llm"] += 1
        else:
            # No LLM available: ambiguous defaults to tier 2 so nothing
            # potentially useful is silently dropped — tune later.
            decided[msg["message_id"]] = (2, "ambiguous (LLM unavailable)")
            stats["fallback"] += 1

    now = enrich.now_iso()
    with conn:
        for message_id, (tier, reason) in decided.items():
            conn.execute(
                """UPDATE messages SET tier = ?, tier_reason = ?, classified_at = ?
                   WHERE message_id = ?""",
                (tier, reason, now, message_id),
            )
            stats[f"tier{tier}"] += 1
            stats["processed"] += 1
    return stats


# --- aggregation -------------------------------------------------------------

def find_consensus(conn, since_hours: float = 24.0) -> list[dict]:
    """Sliding-window consensus: same token from N+ distinct authors inside
    the window. Individual tier doesn't matter — the pattern is the signal."""
    window_min = _C.get("consensus", {}).get("window_minutes", 15)
    min_authors = _C.get("consensus", {}).get("min_authors", 3)
    since = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat(timespec="seconds")

    rows = conn.execute(
        """SELECT m.token_key, m.author, m.mentioned_at, m.message_id,
                  COALESCE(t.alias_of, m.token_key) AS canonical
           FROM mentions m LEFT JOIN tokens t ON t.token_key = m.token_key
           WHERE m.mentioned_at >= ? AND m.author IS NOT NULL
           ORDER BY m.mentioned_at""",
        (since,),
    ).fetchall()

    by_token: dict[str, list] = defaultdict(list)
    for r in rows:
        by_token[r["canonical"]].append(r)

    events = []
    for token_key, ms in by_token.items():
        best = None
        for i, anchor in enumerate(ms):
            t0 = datetime.fromisoformat(anchor["mentioned_at"])
            window = [m for m in ms[i:]
                      if datetime.fromisoformat(m["mentioned_at"]) - t0
                      <= timedelta(minutes=window_min)]
            authors = {m["author"] for m in window}
            if len(authors) >= min_authors and (best is None or len(window) > len(best["window"])):
                best = {"window": window, "authors": authors, "start": anchor["mentioned_at"]}
        total_authors = {m["author"] for m in ms}
        events.append({
            "token_key": token_key,
            "mentions": len(ms),
            "authors": len(total_authors),
            "first_at": ms[0]["mentioned_at"],
            "last_at": ms[-1]["mentioned_at"],
            "consensus": bool(best),
            "consensus_start": best["start"] if best else None,
            "consensus_authors": len(best["authors"]) if best else 0,
            "consensus_mentions": len(best["window"]) if best else 0,
        })
    events.sort(key=lambda e: (e["consensus"], e["authors"], e["mentions"]), reverse=True)
    return events


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    conn = db.connect(CONFIG["db"]["path"])
    print(json.dumps(process_pending(conn), indent=2))
    for ev in find_consensus(conn):
        print(ev)
