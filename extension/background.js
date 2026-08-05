// ============================================================================
// background.js — MV3 service worker.
//
// Owns the durable buffer (chrome.storage.local, survives worker teardown
// and browser restarts), the POST-with-backoff loop, and the toolbar badge.
//
// In console-only mode (the step-1 default) nothing ever leaves the machine:
// batches are logged and buffered up to a cap so you can inspect them.
// ============================================================================

'use strict';

const CFG_KEY = 'dsxConfig';
const BUF_KEY = 'dsxBuffer';
const STATS_KEY = 'dsxStats';

const DEFAULT_CONFIG = {
  enabled: true,
  consoleOnly: true, // step-1 validation mode: never POSTs anywhere
  endpoint: '',
  secret: '',
  allowlist: [], // channel IDs; empty = capture whichever channel is open
  debug: true,
};

const DEFAULT_STATS = {
  captured: 0,
  droppedFromBuffer: 0,
  posted: 0,
  failCount: 0,
  lastCaptureAt: null,
  lastPostAt: null,
  lastError: null,
  selectorsOk: true,
  selectorsDetail: '',
};

const MAX_BUFFER = 5000;
const POST_BATCH = 200;
const BACKOFF_BASE_S = 30;
const BACKOFF_CAP_S = 600;
const RETRY_ALARM = 'dsx-retry';
const TICK_ALARM = 'dsx-tick';
const ALERTS_ALARM = 'dsx-alerts';
const LAST_ALERT_KEY = 'dsxLastAlertId';
const ALERT_URLS_KEY = 'dsxAlertUrls';

// Serialize all buffer/stats read-modify-write cycles so a capture batch
// arriving mid-POST can't clobber the buffer.
let chain = Promise.resolve();
function serialized(fn) {
  const run = chain.then(fn, fn);
  chain = run.catch((e) => console.error('[DSX bg]', e));
  return run;
}

async function getConfig() {
  const r = await chrome.storage.local.get(CFG_KEY);
  return { ...DEFAULT_CONFIG, ...(r[CFG_KEY] || {}) };
}

async function getStats() {
  const r = await chrome.storage.local.get(STATS_KEY);
  return { ...DEFAULT_STATS, ...(r[STATS_KEY] || {}) };
}

async function patchStats(patch) {
  const stats = { ...(await getStats()), ...patch };
  await chrome.storage.local.set({ [STATS_KEY]: stats });
  return stats;
}

async function init() {
  const r = await chrome.storage.local.get([CFG_KEY, STATS_KEY, BUF_KEY]);
  const toSet = {};
  if (!r[CFG_KEY]) toSet[CFG_KEY] = DEFAULT_CONFIG;
  if (!r[STATS_KEY]) toSet[STATS_KEY] = DEFAULT_STATS;
  if (!r[BUF_KEY]) toSet[BUF_KEY] = [];
  if (Object.keys(toSet).length) await chrome.storage.local.set(toSet);
  // Safety-net alarm: retries posting if a one-off retry alarm was lost to a
  // browser restart. No-op in console-only mode or when the buffer is empty.
  chrome.alarms.create(TICK_ALARM, { periodInMinutes: 1 });
  // Alert polling: desktop notifications for signals the processor found.
  chrome.alarms.create(ALERTS_ALARM, { periodInMinutes: 1 });
  await updateBadge();
}

chrome.runtime.onInstalled.addListener(init);
chrome.runtime.onStartup.addListener(init);

// --- ingest from content script ---------------------------------------------

async function onBatch(messages) {
  const cfg = await getConfig();
  if (!cfg.enabled || !Array.isArray(messages) || !messages.length) return;

  await serialized(async () => {
    const r = await chrome.storage.local.get(BUF_KEY);
    let buf = r[BUF_KEY] || [];
    buf = buf.concat(messages);
    let dropped = 0;
    if (buf.length > MAX_BUFFER) {
      dropped = buf.length - MAX_BUFFER;
      buf = buf.slice(dropped); // drop oldest
    }
    await chrome.storage.local.set({ [BUF_KEY]: buf });
    const stats = await getStats();
    await patchStats({
      captured: stats.captured + messages.length,
      droppedFromBuffer: stats.droppedFromBuffer + dropped,
      lastCaptureAt: Date.now(),
    });
    if (cfg.debug || cfg.consoleOnly) {
      console.log(`[DSX bg] +${messages.length} messages (buffer ${buf.length})`, messages);
    }
  });

  await updateBadge();
  if (!cfg.consoleOnly && cfg.endpoint) tryPost();
}

// --- POST with exponential backoff -------------------------------------------

async function tryPost() {
  const cfg = await getConfig();
  if (cfg.consoleOnly || !cfg.endpoint) return;

  await serialized(async () => {
    for (;;) {
      const r = await chrome.storage.local.get(BUF_KEY);
      const buf = r[BUF_KEY] || [];
      if (!buf.length) break;
      const slice = buf.slice(0, POST_BATCH);
      try {
        const res = await fetch(cfg.endpoint, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-Capture-Auth': cfg.secret || '',
          },
          body: JSON.stringify({ messages: slice, sentAt: new Date().toISOString() }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        await chrome.storage.local.set({ [BUF_KEY]: buf.slice(slice.length) });
        const stats = await getStats();
        await patchStats({
          posted: stats.posted + slice.length,
          lastPostAt: Date.now(),
          failCount: 0,
          lastError: null,
        });
      } catch (e) {
        const stats = await getStats();
        const failCount = stats.failCount + 1;
        await patchStats({ failCount, lastError: String(e) });
        const backoffS =
          Math.min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** Math.min(failCount - 1, 6)) *
          (0.75 + Math.random() * 0.5);
        chrome.alarms.create(RETRY_ALARM, { delayInMinutes: Math.max(0.5, backoffS / 60) });
        console.warn(`[DSX bg] POST failed (${e}); retry in ~${Math.round(backoffS)}s`);
        break;
      }
    }
  });
  await updateBadge();
}

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === RETRY_ALARM) {
    tryPost();
  } else if (alarm.name === TICK_ALARM) {
    const cfg = await getConfig();
    if (cfg.consoleOnly || !cfg.endpoint) return;
    const r = await chrome.storage.local.get(BUF_KEY);
    if ((r[BUF_KEY] || []).length) tryPost();
  } else if (alarm.name === ALERTS_ALARM) {
    pollAlerts();
  }
});

// --- alert notifications ------------------------------------------------------

async function pollAlerts() {
  const cfg = await getConfig();
  if (cfg.consoleOnly || !cfg.endpoint || !cfg.enabled) return;
  const base = cfg.endpoint.replace(/\/ingest\/?$/, '');
  const r = await chrome.storage.local.get([LAST_ALERT_KEY, ALERT_URLS_KEY]);
  const after = r[LAST_ALERT_KEY] || 0;
  let data;
  try {
    const res = await fetch(`${base}/alerts?after=${after}`, {
      headers: { 'X-Capture-Auth': cfg.secret || '' },
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    data = await res.json();
  } catch (e) {
    console.warn('[DSX bg] alert poll failed:', String(e));
    return;
  }
  if (!data.alerts || !data.alerts.length) return;

  const urls = r[ALERT_URLS_KEY] || {};
  let lastId = after;
  for (const alert of data.alerts) {
    lastId = Math.max(lastId, alert.id);
    const notifId = `dsx-${alert.id}`;
    if (alert.url) urls[notifId] = alert.url;
    chrome.notifications.create(notifId, {
      type: 'basic',
      iconUrl: 'icon128.png',
      title: alert.title || 'Signal',
      message: (alert.body || '').slice(0, 500),
      priority: alert.kind === 'tier1' || alert.kind === 'consensus' ? 2 : 0,
      requireInteraction: alert.kind === 'consensus',
    });
  }
  // keep the click-through map small
  const keys = Object.keys(urls);
  if (keys.length > 50) for (const k of keys.slice(0, keys.length - 50)) delete urls[k];
  await chrome.storage.local.set({ [LAST_ALERT_KEY]: lastId, [ALERT_URLS_KEY]: urls });
}

chrome.notifications.onClicked.addListener(async (notifId) => {
  const r = await chrome.storage.local.get(ALERT_URLS_KEY);
  const url = (r[ALERT_URLS_KEY] || {})[notifId];
  if (url) chrome.tabs.create({ url });
  chrome.notifications.clear(notifId);
});

// --- badge -------------------------------------------------------------------

async function updateBadge() {
  const [cfg, stats, r] = await Promise.all([
    getConfig(),
    getStats(),
    chrome.storage.local.get(BUF_KEY),
  ]);
  const buffered = (r[BUF_KEY] || []).length;

  let text = '';
  let color = '#3ba55d'; // green: healthy
  if (!cfg.enabled) {
    text = 'off';
    color = '#747f8d';
  } else if (!stats.selectorsOk) {
    text = 'ERR';
    color = '#ed4245';
  } else if (buffered > 0) {
    text = buffered > 999 ? '1k+' : String(buffered);
    color = cfg.consoleOnly ? '#747f8d' : stats.failCount > 0 ? '#faa61a' : '#3ba55d';
  }
  await chrome.action.setBadgeText({ text });
  await chrome.action.setBadgeBackgroundColor({ color });
}

// --- message routing ---------------------------------------------------------

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    if (!msg || !msg.type) return sendResponse({ ok: false });
    switch (msg.type) {
      case 'dsx-batch':
        await onBatch(msg.messages);
        return sendResponse({ ok: true });
      case 'dsx-health': {
        await patchStats({ selectorsOk: !!msg.ok, selectorsDetail: msg.detail || '' });
        if (!msg.ok) console.error('[DSX bg] SELECTORS BROKEN:', msg.detail);
        await updateBadge();
        return sendResponse({ ok: true });
      }
      case 'dsx-status': {
        const [cfg, stats, r] = await Promise.all([
          getConfig(),
          getStats(),
          chrome.storage.local.get(BUF_KEY),
        ]);
        return sendResponse({ config: cfg, stats, buffered: (r[BUF_KEY] || []).length });
      }
      case 'dsx-save-config': {
        const cfg = { ...(await getConfig()), ...msg.config };
        await chrome.storage.local.set({ [CFG_KEY]: cfg });
        await updateBadge();
        return sendResponse({ ok: true, config: cfg });
      }
      case 'dsx-post-now':
        await tryPost();
        return sendResponse({ ok: true });
      case 'dsx-clear-buffer': {
        await serialized(() => chrome.storage.local.set({ [BUF_KEY]: [] }));
        await updateBadge();
        return sendResponse({ ok: true });
      }
      default:
        return sendResponse({ ok: false });
    }
  })();
  return true; // keep the message channel open for the async response
});

init();
