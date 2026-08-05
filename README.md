# discord-signal

Signal extraction pipeline for a high-noise memecoin Discord channel.

- `extension/` — capture client (browser, MV3). Passive read-only DOM observer.
  **Status: built, awaiting validation against the real channel.**
- `server/` — processor (VPS, Python). Ingest → dedupe → entity extraction →
  on-chain enrichment → classification → aggregation → Telegram.
  **Status: not started — blocked on capture-client validation (build order step 1).**

Build order and full spec live in the original project brief. Current step:
load `extension/` unpacked, validate extraction in console-only mode per
`extension/README.md`, save a DOM fixture into `extension/fixtures/`.
