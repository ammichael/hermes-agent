import crypto from 'node:crypto';
import path from 'node:path';
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';

const DEFAULT_TTL_SECONDS = 6 * 60 * 60;
const MAX_TTL_SECONDS = 14 * 24 * 60 * 60;

export function temporaryInteractionStatePath() {
  const hermesHome = process.env.HERMES_HOME || path.join(process.env.HOME || '', '.hermes');
  return path.join(hermesHome, 'state', 'whatsapp-temporary-interactions.json');
}

function jid(value) {
  const raw = String(value || '').trim().toLowerCase().replace(/:.*@/, '@');
  const id = raw.replace(/@.*/, '').replace(/^\+/, '');
  if (!id) return '';
  const suffix = raw.endsWith('@g.us')
    ? '@g.us'
    : (raw.endsWith('@lid') ? '@lid' : '@s.whatsapp.net');
  return `${id}${suffix}`;
}

function load(statePath) {
  if (!existsSync(statePath)) return { schema_version: 2, grants: [] };
  try {
    const parsed = JSON.parse(readFileSync(statePath, 'utf8'));
    return parsed?.schema_version === 2 && Array.isArray(parsed.grants)
      ? parsed
      : { schema_version: 2, grants: [] };
  } catch {
    return { schema_version: 2, grants: [] };
  }
}

function save(statePath, data) {
  mkdirSync(path.dirname(statePath), { recursive: true });
  const tmp = `${statePath}.${process.pid}.tmp`;
  writeFileSync(tmp, `${JSON.stringify(data, null, 2)}\n`, { encoding: 'utf8', mode: 0o600 });
  renameSync(tmp, statePath);
}

function active(grant, nowMs) {
  const expiresMs = Date.parse(String(grant?.expires_at || ''));
  return grant?.kind === 'temporary_interaction'
    && grant?.enabled === true
    && Array.isArray(grant?.capabilities)
    && grant.capabilities.length === 1
    && grant.capabilities[0] === 'text_reply'
    && Array.isArray(grant?.delivery_message_ids)
    && grant.delivery_message_ids.length > 0
    && typeof grant?.topic === 'string'
    && grant.topic.trim()
    && Number.isFinite(expiresMs)
    && expiresMs > nowMs;
}

export function createTemporaryInteractionGrant({
  statePath = temporaryInteractionStatePath(),
  chatJid,
  participantJids = [],
  topic,
  ttlSeconds = DEFAULT_TTL_SECONDS,
  deliveryMessageIds,
  nowMs = Date.now(),
}) {
  const validated = validateTemporaryInteractionRequest({
    chatJid, participantJids, topic, ttlSeconds,
  });
  const exactChatJid = jid(chatJid);
  if (!Array.isArray(deliveryMessageIds) || deliveryMessageIds.length === 0) {
    throw new Error('invalid temporary interaction grant');
  }

  const data = load(statePath);
  data.grants = data.grants.filter((grant) => active(grant, nowMs));
  const grant = {
    id: crypto.randomUUID(),
    kind: 'temporary_interaction',
    enabled: true,
    chat_jid: exactChatJid,
    participant_jids: validated.participantJids,
    topic: validated.topic,
    created_at: new Date(nowMs).toISOString(),
    expires_at: new Date(nowMs + validated.ttlSeconds * 1000).toISOString(),
    delivery_message_ids: deliveryMessageIds.map(String).filter(Boolean),
    capabilities: ['text_reply'],
  };
  data.grants = data.grants.filter((item) => jid(item.chat_jid) !== exactChatJid);
  data.grants.push(grant);
  save(statePath, data);
  return grant;
}

export function validateTemporaryInteractionRequest({
  chatJid,
  participantJids = [],
  topic,
  ttlSeconds = DEFAULT_TTL_SECONDS,
}) {
  const exactChatJid = jid(chatJid);
  const participants = [...new Set([exactChatJid, ...participantJids.map(jid)].filter(Boolean))];
  const ttl = Number(ttlSeconds);
  if (
    !exactChatJid
    || exactChatJid.endsWith('@g.us')
    || !Array.isArray(participantJids)
    || participants.length === 0
    || !String(topic || '').trim()
    || !Number.isFinite(ttl)
    || ttl <= 0
    || ttl > MAX_TTL_SECONDS
  ) {
    throw new Error('invalid temporary interaction request');
  }
  return {
    chatJid: exactChatJid,
    participantJids: participants,
    topic: String(topic).trim(),
    ttlSeconds: ttl,
  };
}

export function findTemporaryInteractionGrant({
  statePath = temporaryInteractionStatePath(),
  chatJid,
  senderJid,
  isGroup,
  isTextOnly,
  nowMs = Date.now(),
}) {
  if (isGroup || !isTextOnly) return null;
  const exactChatJid = jid(chatJid);
  const exactSenderJid = jid(senderJid);
  const data = load(statePath);
  return data.grants.find((grant) => (
    active(grant, nowMs)
    && jid(grant.chat_jid) === exactChatJid
    && (grant.participant_jids || []).map(jid).includes(exactSenderJid)
  )) || null;
}
