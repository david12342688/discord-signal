# Discord Signal Capture — capture client

Passive, read-only MV3 extension. It observes the Discord message list your
browser has **already rendered** in your own logged-in tab and forwards new
messages to your processor. It never talks to Discord: no API calls, no
tokens, no injected requests, no page mutations. The only page interaction in
the codebase is the optional, user-triggered, one-shot backfill scroll.

## Files

| File | Role |
|---|---|
| `manifest.json` | MV3 manifest. Host permission for your VPS is **optional** and requested only when you configure an endpoint. |
| `selectors.js` | **All Discord DOM knowledge.** When Discord ships a release and capture breaks, fix this file and nothing else. |
| `capture.js` | MutationObserver capture loop, dedupe/edit detection, 2s batching, backfill. |
| `background.js` | Durable buffer (`chrome.storage.local`), POST with exponential backoff, toolbar badge. |
| `popup.html/js` | On/off, console-only mode, channel allowlist, endpoint + secret, status, buffered count, backfill trigger. |
| `fixtures/` | Saved channel DOM for extraction tests (see below). |

## Install (unpacked)

1. Chrome/Brave/Edge → `chrome://extensions`
2. Enable **Developer mode** (top right)
3. **Load unpacked** → select this `extension/` directory
4. Pin the extension so you can see the badge

## Step-1 validation (no server needed)

The extension ships with **console-only mode ON** — nothing leaves your
machine until you turn it off.

1. Open Discord in a tab and navigate to your channel.
2. Open DevTools (F12) → Console tab. You should see:
   - `[DSX] capture active, config loaded`
   - `[DSX] attached to scroller, channel <id>`
   - one `[DSX] capture <messageId> <author> — <text>` line per already-rendered message
3. Wait for new messages to arrive live — each should log within ~2s.
4. Check the popup: "Watching channel …", Selectors OK, captured/buffered counts climbing.
5. Background-tab check: switch to another tab for a few minutes while the
   channel is active, come back, confirm the captured count kept climbing.
6. Optional: set the channel allowlist in the popup (channel ID = the second
   long number in the URL, `/channels/<guild>/<channel>`) and confirm other
   channels stop logging.

Things to eyeball in the captured objects:
- `author` is right on **grouped messages** (consecutive messages from one
  person, where Discord hides the name header)
- `timestamp` is ISO from the `datetime` attribute; `timestampApprox: true`
  means Discord rendered no `<time>` node and the processor should fall back
  to `capturedAt`
- edits show up again with `edited: true`
- replies carry a `reply.preview`

Known intentional gap: attachment-only messages (image, no text, no embed, no
link) are skipped — there's nothing for the pipeline to extract.

## Badge

- green count — capturing, posting healthy
- gray count — capturing in console-only mode
- orange count — capturing, but POSTs failing (retrying with backoff)
- red `ERR` — **selectors broken**: the message list is present but nothing
  parses. Discord changed its DOM. Fix `selectors.js`.
- `off` — capture disabled

## Backfill

One-shot and user-triggered only, from the popup. Scrolls the open channel
back in ~0.85-viewport steps with randomized 2–5s pauses until it reaches the
requested age (default 24h), the top of history, or a hard 300-step cap.
Keep the tab **foregrounded** while it runs — background tabs throttle the
timers that pace it. It never runs automatically and never loops.

## Connecting the processor (step 2, later)

Popup → uncheck console-only, set endpoint (`https://…/ingest`) and the shared
secret, Save. The browser will ask to grant host access to that origin —
that's the optional host permission, scoped to just your VPS. Buffered
messages drain on the next successful POST; the buffer holds 5,000 messages
across restarts, oldest dropped first if it overflows (the popup shows a
dropped counter if that ever happens).

## Fixtures for tests

To let the extraction logic be tested offline: with your channel open,
DevTools → Console →

```js
copy(document.querySelector('[data-list-id="chat-messages"]').outerHTML)
```

then paste into `fixtures/channel-YYYY-MM-DD.html`. Grab a slice that
includes: a normal message, a grouped message (no author header), a reply, a
message with an embed, and a link. Scrub anything you don't want in a repo.

## Design notes / gotchas

- **Timers are not trusted.** Background tabs throttle `setTimeout` to as
  little as once per minute, but `MutationObserver` keeps firing. The flush
  is therefore mutation-driven ("has it been ≥2s since last flush? flush
  inline"), with the timer only as a tail-flush for quiet periods.
- **Virtualization.** Discord unmounts off-screen messages. Everything is
  extracted at insert time; nodes are never revisited.
- **Dedupe key** is the Discord snowflake from the `<li>` id, so tab
  reloads, channel re-entry, and backfill overlap are all harmless. A
  content-hash LRU (4,000 entries) additionally distinguishes "same message
  re-rendered" (dropped) from "message edited" (re-sent with `edited: true`).
- **SPA lifecycle.** The scroller is unmounted on every channel switch; a
  body-level observer re-finds and re-attaches automatically.
