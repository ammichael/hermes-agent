import path from 'node:path';
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';

const MAX_EVENTS_PER_WATCH = 100;

function normalizeJid(value) {
  const raw = String(value || '').trim().toLowerCase().replace(/:.*@/, '@');
  const id = raw.replace(/@.*/, '').replace(/^\+/, '');
  if (!id) return '';
  return `${id}${raw.endsWith('@lid') ? '@lid' : (raw.endsWith('@g.us') ? '@g.us' : '@s.whatsapp.net')}`;
}

function load(statePath) {
  if (!existsSync(statePath)) return { schema_version: 1, watches: [] };
  try {
    const parsed = JSON.parse(readFileSync(statePath, 'utf8'));
    return parsed?.schema_version === 1 && Array.isArray(parsed.watches)
      ? parsed
      : { schema_version: 1, watches: [] };
  } catch {
    return { schema_version: 1, watches: [] };
  }
}

function save(statePath, data) {
  mkdirSync(path.dirname(statePath), { recursive: true });
  const temp = `${statePath}.${process.pid}.tmp`;
  writeFileSync(temp, `${JSON.stringify(data, null, 2)}\n`, { encoding: 'utf8', mode: 0o600 });
  renameSync(temp, statePath);
}

function active(watch, nowMs) {
  const expiry = Date.parse(String(watch?.expires_at || ''));
  return watch?.enabled === true
    && typeof watch?.id === 'string'
    && normalizeJid(watch?.chat_jid)
    && Number.isFinite(expiry)
    && expiry > nowMs;
}

export function recordReadOnlyReplyWatch({
  statePath = path.join(process.env.HERMES_HOME || path.join(process.env.HOME || '', '.hermes'), 'state', 'whatsapp-reply-watches.json'),
  chatJid,
  senderJid,
  messageId,
  timestampMs,
  body,
  isGroup,
  fromMe,
  nowMs = Date.now(),
}) {
  if (isGroup || fromMe || !messageId || !normalizeJid(senderJid)) return { recorded: false };
  const exactChat = normalizeJid(chatJid);
  const data = load(statePath);
  const watch = data.watches.find((item) => {
    if (!active(item, nowMs)) return false;
    const aliases = new Set([
      normalizeJid(item.chat_jid),
      ...(item.participant_jids || []).map(normalizeJid),
    ]);
    return aliases.has(exactChat);
  });
  if (!watch || (watch.events || []).some((event) => event.message_id === String(messageId))) return { recorded: false };

  const event = {
    message_id: String(messageId),
    sender_jid: normalizeJid(senderJid),
    received_at: new Date(Number.isFinite(timestampMs) ? timestampMs : nowMs).toISOString(),
    body: String(body || '').slice(0, 3500),
  };
  watch.events = [...(watch.events || []), event].slice(-MAX_EVENTS_PER_WATCH);
  watch.last_seen_at = event.received_at;
  save(statePath, data);
  return { recorded: true, watchId: watch.id };
}
