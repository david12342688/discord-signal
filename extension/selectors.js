// ============================================================================
// selectors.js — ALL Discord DOM knowledge lives in this one file.
//
// When Discord ships a release and capture breaks, this is the only file you
// should need to touch. Everything else treats messages as opaque objects.
//
// Strategy: never select on obfuscated CSS class names (they rotate between
// releases). Anchor on structural attributes Discord has kept stable for
// years because its own accessibility and list-virtualization code depends
// on them:
//
//   - scroller:      [data-list-id="chat-messages"]
//   - message <li>:  id="chat-messages-<channelId>-<messageId>"
//   - content node:  id="message-content-<messageId>"
//   - username node: id="message-username-<messageId>"
//   - timestamps:    <time datetime="...">  (machine-readable, not rendered text)
//   - reply header:  id="message-reply-context-<messageId>"
//   - accessories:   id="message-accessories-<messageId>" (embeds/attachments)
//
// The <li> id doubles as our dedupe key: the embedded messageId is a Discord
// snowflake — globally unique and time-ordered.
// ============================================================================

(() => {
  'use strict';

  const sel = {
    version: '2026-08-05',
    scroller: '[data-list-id="chat-messages"]',
    messageItem: 'li[id^="chat-messages-"]',
    content: '[id^="message-content-"]',
    username: '[id^="message-username-"]',
    timestamp: 'time[datetime]',
    replyContext: '[id^="message-reply-context-"]',
    accessories: '[id^="message-accessories-"]',
    // Embeds render as <article> elements inside the accessories container.
    // Tag-based, not class-based, so it survives class rotation.
    embed: 'article',
  };

  const LIST_ITEM_ID_RE = /^chat-messages-(\d+)-(\d+)$/;

  function parseListItemId(li) {
    const m = LIST_ITEM_ID_RE.exec(li.id || '');
    if (!m) return null; // divider, system message, "new messages" bar, etc.
    return { channelId: m[1], messageId: m[2] };
  }

  function textOf(el) {
    return el ? el.textContent.replace(/\s+/g, ' ').trim() : '';
  }

  // Grouped messages (same author, short interval) omit the username header.
  // The node is freshly inserted at capture time, so earlier siblings are
  // still mounted — walk back to the nearest <li> that has one.
  function findAuthor(li) {
    const direct = li.querySelector(sel.username);
    if (direct) return textOf(direct);
    let prev = li.previousElementSibling;
    let steps = 0;
    while (prev && steps++ < 80) {
      if (LIST_ITEM_ID_RE.test(prev.id || '')) {
        const u = prev.querySelector(sel.username);
        if (u) return textOf(u);
      }
      prev = prev.previousElementSibling;
    }
    return null;
  }

  // Prefer the machine-readable datetime attribute over rendered text.
  // Grouped messages sometimes lack a <time> node; caller falls back to
  // capture time and marks it approximate.
  function findTimestamp(li) {
    const t = li.querySelector(sel.timestamp);
    return t ? t.getAttribute('datetime') : null;
  }

  function findReplyContext(li) {
    const ctx = li.querySelector(sel.replyContext);
    if (!ctx) return null;
    return { preview: textOf(ctx) };
  }

  function findEmbeds(li) {
    const acc = li.querySelector(sel.accessories);
    if (!acc) return [];
    const articles = Array.from(acc.querySelectorAll(sel.embed))
      .map(textOf)
      .filter(Boolean);
    if (articles.length) return articles;
    // Accessories with no <article> (e.g. plain link cards): fall back to the
    // container's own text so we don't lose it entirely.
    const fallback = textOf(acc);
    return fallback ? [fallback] : [];
  }

  // Raw hrefs matter: shortened/pretty-printed link text can hide the real
  // contract address or Dexscreener URL.
  function findLinks(li) {
    const out = [];
    for (const a of li.querySelectorAll('a[href]')) {
      const href = a.href;
      if (href && href.startsWith('http') && !out.includes(href)) out.push(href);
    }
    return out;
  }

  // Returns a plain message object, or null if this <li> isn't a capturable
  // message (system rows, dividers, attachment-only posts with no text).
  function extractMessage(li) {
    const idInfo = parseListItemId(li);
    if (!idInfo) return null;

    const ts = findTimestamp(li);
    const msg = {
      messageId: idInfo.messageId,
      channelId: idInfo.channelId,
      author: findAuthor(li),
      timestamp: ts,
      timestampApprox: ts === null,
      content: textOf(li.querySelector(sel.content)),
      embeds: findEmbeds(li),
      links: findLinks(li),
      reply: findReplyContext(li),
      capturedAt: new Date().toISOString(),
      selectorVersion: sel.version,
    };

    if (!msg.content && !msg.embeds.length && !msg.links.length) return null;
    return msg;
  }

  // Health probe: distinguish "channel is quiet" from "selectors are broken".
  // If the scroller has plenty of <li> rows but zero parse as messages,
  // Discord changed its DOM contract — fail loudly upstream.
  function probeHealth(scroller) {
    const rows = scroller.querySelectorAll('li').length;
    const parsed = Array.from(scroller.querySelectorAll(sel.messageItem))
      .filter((li) => parseListItemId(li)).length;
    if (rows >= 5 && parsed === 0) {
      return { ok: false, detail: `${rows} list rows but 0 parseable messages — selectors broken (v${sel.version})` };
    }
    return { ok: true, detail: `${parsed}/${rows} rows parseable` };
  }

  window.__DSX_SELECTORS__ = { sel, parseListItemId, extractMessage, probeHealth };
})();
