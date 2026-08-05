# Processor (VPS)

## Status: full pipeline live

```
server/
  ingest.py    HTTP app: POST /ingest, GET /alerts, GET /digest, GET /health
               + background pipeline thread (classify/enrich/alert every 30s)
  extract.py   entity extraction (contracts, tickers, URLs) + snowflake time
  enrich.py    Dexscreener/RugCheck clients, caching, rate limits, red flags
  classify.py  heuristics -> OpenRouter LLM triage -> consensus aggregation
  notify.py    alert generation (tier1/consensus/watchdog/digest), rate caps
  digest.py    ranked 24h digest (data + markdown)
  db.py        SQLite connection + schema bootstrap
  schema.sql   messages / tokens / mentions / alerts_sent
  config.yaml  every threshold
  .env         secrets (gitignored) — copy from .env.example
  start.sh     cron @reboot entrypoint (flock single-instance)
  discord-signal.service  reference systemd unit for when sudo is available
```

Alert delivery: the browser extension polls GET /alerts once a minute and
fires desktop notifications; clicking opens the Dexscreener page. The daily
digest lives at GET /digest?key=<INGEST_SECRET>.

Run:

```bash
cd server
.venv/bin/uvicorn ingest:app --host 0.0.0.0 --port 8787
```

Quick checks:

```bash
curl http://127.0.0.1:8787/health
# POST requires the X-Capture-Auth header matching INGEST_SECRET in .env
```

Runs under cron @reboot via start.sh (no sudo on this box). TLS remains the
one open hardening item — needs sudo for Caddy/certbot on 80/443; until then
the transport is HTTP + shared secret.
