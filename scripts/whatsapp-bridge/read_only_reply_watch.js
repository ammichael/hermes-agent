import { existsSync, readFileSync, renameSync, writeFileSync } from 'fs';
import path from 'path';
import { normalizeWhatsAppIdentifier } from './allowlist.js';

function activeWatch(watch, nowMs) {
  if (!watch || watch.enabled !== true || watch.notification_only !== true || watch.auto_reply !== false) return false;
  const expiresAt = Date.parse(watch.expires_at || '');
  return Number.isFinite(expiresAt) && expiresAt > nowMs;
}

function aliasesForWatch(watch) {
  return new Set(
    [watch.chat_jid, ...(Array.isArray(watch.participant_jids) ? watch.participant_jids : [])]
      .map(normalizeWhatsAppIdentifier)
      .filter(Boolean),
  );
}

export function findReadOnlyReplyWatch({ watchesPath, senderId, nowMs = Date.now() }) {
  if (!existsSync(watchesPath)) return null;
  try {
    const data = JSON.parse(readFileSync(watchesPath, 'utf8'));
    const sender = normalizeWhatsAppIdentifier(senderId);
    return (data.watches || []).find((watch) => activeWatch(watch, nowMs) && aliasesForWatch(watch).has(sender)) || null;
  } catch {
    return null;
  }
}

export function recordReadOnlyReplyWatchEvent({ watchesPath, watchId, event, nowMs = Date.now() }) {
  if (!watchId || !event?.message_id || !existsSync(watchesPath)) return false;
  try {
    const data = JSON.parse(readFileSync(watchesPath, 'utf8'));
    const watch = (data.watches || []).find((item) => item?.id === watchId);
    if (!activeWatch(watch, nowMs)) return false;
    watch.events = Array.isArray(watch.events) ? watch.events : [];
    if (watch.events.some((item) => item?.message_id === event.message_id)) return true;
    watch.events.push(event);
    const tempPath = `${watchesPath}.tmp`;
    writeFileSync(tempPath, `${JSON.stringify(data, null, 2)}\n`, { mode: 0o600 });
    renameSync(tempPath, watchesPath);
    return true;
  } catch {
    return false;
  }
}

export function defaultReadOnlyWatchesPath(homeDir) {
  return path.join(homeDir, '.hermes', 'state', 'whatsapp-reply-watches.json');
}
