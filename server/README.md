# Processor (VPS)

## Current status: step 2 — ingest + storage

```
server/
  ingest.py    POST /ingest (auth, validation, dedupe), GET /health
  db.py        SQLite connection + schema bootstrap
  schema.sql   messages / tokens / mentions / alerts_sent
  config.yaml  all tunables
  .env         secrets (gitignored) — copy from .env.example
```

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

TLS, systemd unit, and full deploy docs land in build step 6. Until then the
pipe runs HTTP on :8787 with the shared secret — temporary, for the
end-to-end test only.
