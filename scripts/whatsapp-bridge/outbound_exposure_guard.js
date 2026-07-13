export class OutboundExposureError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'OutboundExposureError';
    this.code = code;
  }
}

function textFingerprint(chatId, payload) {
  const text = typeof payload?.text === 'string' ? payload.text.trim() : '';
  if (!text || payload?.edit) return null;
  return `${chatId}\u0000${text}`;
}

function isInternalOperationalText(payload) {
  const text = typeof payload?.text === 'string' ? payload.text.trim() : '';
  if (!text) return false;
  return (
    text.startsWith('⚡ Interrupting current task. I\'ll respond to your message shortly.')
    || text.startsWith('Operation interrupted: waiting for model response (')
    || /^Background process proc_[A-Za-z0-9]+ (?:finished|failed)\b/.test(text)
  );
}

/**
 * In-memory circuit breaker for a single WhatsApp bridge process.
 * It limits runaway loops without persisting message bodies or recipient IDs.
 */
export function createOutboundExposureGuard({
  now = () => Date.now(),
  sleep = ms => new Promise(resolve => setTimeout(resolve, ms)),
  windowMs = 10 * 60 * 1000,
  globalLimit = 60,
  perChatLimit = 12,
  duplicateTtlMs = 2 * 60 * 1000,
  minimumSpacingMs = 750,
} = {}) {
  let globalSends = [];
  const chatSends = new Map();
  const duplicates = new Map();
  let lastSendAt = 0;

  function prune(at) {
    const cutoff = at - windowMs;
    globalSends = globalSends.filter(ts => ts > cutoff);
    for (const [chatId, timestamps] of chatSends) {
      const kept = timestamps.filter(ts => ts > cutoff);
      if (kept.length) chatSends.set(chatId, kept);
      else chatSends.delete(chatId);
    }
    for (const [fingerprint, ts] of duplicates) {
      if (ts <= at - duplicateTtlMs) duplicates.delete(fingerprint);
    }
  }

  async function beforeSend(chatId, payload) {
    if (isInternalOperationalText(payload)) {
      throw new OutboundExposureError(
        'operational_metadata_suppressed',
        'internal operational metadata suppressed from WhatsApp',
      );
    }
    let at = now();
    const waitMs = Math.max(0, minimumSpacingMs - (at - lastSendAt));
    if (waitMs > 0) {
      await sleep(waitMs);
      at = now();
    }
    prune(at);

    const fingerprint = textFingerprint(chatId, payload);
    if (fingerprint && duplicates.has(fingerprint)) {
      throw new OutboundExposureError(
        'duplicate_suppressed',
        'duplicate WhatsApp message suppressed by exposure guard',
      );
    }
    if (globalSends.length >= globalLimit) {
      throw new OutboundExposureError(
        'global_rate_limited',
        'global WhatsApp outbound limit reached',
      );
    }
    const perChat = chatSends.get(chatId) || [];
    if (perChat.length >= perChatLimit) {
      throw new OutboundExposureError(
        'chat_rate_limited',
        'per-chat WhatsApp outbound limit reached',
      );
    }

    globalSends.push(at);
    perChat.push(at);
    chatSends.set(chatId, perChat);
    if (fingerprint) duplicates.set(fingerprint, at);
    lastSendAt = at;
  }

  function snapshot() {
    const at = now();
    prune(at);
    return {
      globalWindowCount: globalSends.length,
      activeChats: chatSends.size,
      duplicateFingerprints: duplicates.size,
      limits: { windowMs, globalLimit, perChatLimit, duplicateTtlMs, minimumSpacingMs },
    };
  }

  return { beforeSend, snapshot };
}
