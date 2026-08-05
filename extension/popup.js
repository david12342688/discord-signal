'use strict';

const $ = (id) => document.getElementById(id);

function fmtTime(ts) {
  if (!ts) return 'never';
  const s = Math.round((Date.now() - ts) / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

async function activeDiscordTab() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  return tabs[0] || null;
}

async function refreshStatus() {
  const res = await chrome.runtime.sendMessage({ type: 'dsx-status' }).catch(() => null);
  if (!res) return;
  const { config, stats, buffered } = res;

  $('stSelectors').textContent = stats.selectorsOk
    ? `Selectors OK ${stats.selectorsDetail ? '(' + stats.selectorsDetail + ')' : ''}`
    : `SELECTORS BROKEN — ${stats.selectorsDetail}`;
  $('stSelectors').className = stats.selectorsOk ? 'ok' : 'err';

  $('stCaptured').textContent = `Captured: ${stats.captured} total · last ${fmtTime(stats.lastCaptureAt)}`;
  $('stBuffered').textContent =
    `Buffered: ${buffered}` +
    (stats.droppedFromBuffer ? ` · ${stats.droppedFromBuffer} dropped (buffer cap)` : '');
  $('stBuffered').className = stats.droppedFromBuffer ? 'warn' : '';

  $('stPost').textContent = config.consoleOnly
    ? 'Console-only mode — nothing is sent anywhere'
    : `Posted: ${stats.posted} · last ${fmtTime(stats.lastPostAt)}`;
  $('stPost').className = config.consoleOnly ? 'muted' : '';

  $('stError').textContent = stats.lastError
    ? `Last error (${stats.failCount} consecutive): ${stats.lastError}`
    : '';
  $('stError').className = 'warn';

  // Ping the content script in the active tab.
  const tab = await activeDiscordTab();
  if (tab) {
    const ping = await chrome.tabs
      .sendMessage(tab.id, { type: 'dsx-ping' })
      .catch(() => null);
    if (ping && ping.alive) {
      $('stTab').textContent = ping.attached
        ? `Watching channel ${ping.channelId || '?'}${ping.backfillRunning ? ' · backfill running' : ''}`
        : 'On Discord, but no message list found in this tab';
      $('stTab').className = ping.attached ? 'ok' : 'warn';
      $('backfill').disabled = !ping.attached || ping.backfillRunning;
    } else {
      $('stTab').textContent = 'Active tab is not a Discord channel';
      $('stTab').className = 'muted';
      $('backfill').disabled = true;
    }
  }
}

async function loadConfig() {
  const res = await chrome.runtime.sendMessage({ type: 'dsx-status' });
  const c = res.config;
  $('enabled').checked = c.enabled;
  $('consoleOnly').checked = c.consoleOnly;
  $('allowlist').value = (c.allowlist || []).join('\n');
  $('endpoint').value = c.endpoint || '';
  $('secret').value = c.secret || '';
}

$('save').addEventListener('click', async () => {
  const endpoint = $('endpoint').value.trim();
  const config = {
    enabled: $('enabled').checked,
    consoleOnly: $('consoleOnly').checked,
    endpoint,
    secret: $('secret').value,
    allowlist: $('allowlist')
      .value.split(/[\n,]/)
      .map((s) => s.trim())
      .filter((s) => /^\d+$/.test(s)),
  };

  // Posting to the VPS needs host permission for that origin; request it
  // here inside the click gesture.
  if (endpoint && !config.consoleOnly) {
    try {
      const origin = new URL(endpoint).origin + '/*';
      const granted = await chrome.permissions.request({ origins: [origin] });
      if (!granted) {
        $('saveMsg').textContent = 'Saved, but host permission was denied — posting will fail.';
        $('saveMsg').className = 'warn';
      }
    } catch {
      $('saveMsg').textContent = 'Invalid endpoint URL';
      $('saveMsg').className = 'err';
      return;
    }
  }

  await chrome.runtime.sendMessage({ type: 'dsx-save-config', config });
  if (!$('saveMsg').textContent) {
    $('saveMsg').textContent = 'Saved';
    $('saveMsg').className = 'ok';
  }
  setTimeout(() => { $('saveMsg').textContent = ''; }, 2500);
  refreshStatus();
});

$('postNow').addEventListener('click', async () => {
  await chrome.runtime.sendMessage({ type: 'dsx-post-now' });
  refreshStatus();
});

$('clearBuf').addEventListener('click', async () => {
  await chrome.runtime.sendMessage({ type: 'dsx-clear-buffer' });
  refreshStatus();
});

$('backfill').addEventListener('click', async () => {
  const tab = await activeDiscordTab();
  if (!tab) return;
  const hours = Math.max(1, Math.min(72, Number($('backfillHours').value) || 24));
  const res = await chrome.tabs
    .sendMessage(tab.id, { type: 'dsx-backfill', hours })
    .catch(() => null);
  if (res && !res.ok) {
    $('saveMsg').textContent = `Backfill: ${res.reason}`;
    $('saveMsg').className = 'warn';
  }
  refreshStatus();
});

loadConfig().then(refreshStatus);
setInterval(refreshStatus, 1500);
