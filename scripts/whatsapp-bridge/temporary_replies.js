// Capture-only admission. This module never calls the socket or the agent.
import path from 'node:path';
import os from 'node:os';
import {
  constants, openSync, closeSync, fstatSync, readFileSync, writeSync, readSync,
  fsyncSync, mkdirSync,
} from 'node:fs';
import { getMessageContent } from './bridge_helpers.js';

const MAX_STATE = 256 * 1024;
const MAX_INBOX = 8 * 1024 * 1024;
const WEEK = 7 * 24 * 60 * 60 * 1000;
const id = value => typeof value === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(value);
const text = (value, max) => typeof value === 'string' && value.length > 0 && Buffer.byteLength(value) <= max;
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);

export function temporaryReplyHome() {
  return process.env.HERMES_HOME || path.join(os.homedir(), '.hermes');
}

function readBounded(file, max, privateFile = false) {
  const fd = openSync(file, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const stat = fstatSync(fd);
    if (!stat.isFile() || stat.size > max || (privateFile &&
        ((stat.mode & 0o077) || (process.getuid && stat.uid !== process.getuid())))) {
      throw new Error('unsafe_state');
    }
    const bytes = readFileSync(fd);
    if (bytes.length > max) throw new Error('oversized_state');
    return bytes.toString('utf8');
  } finally { closeSync(fd); }
}

export function findTemporaryReplyGrant(msg, { home = temporaryReplyHome(), sessionDir, now = Date.now() }) {
  const key = msg?.key;
  if (!key || key.fromMe !== false || typeof key.remoteJid !== 'string' ||
      !/^\d{5,20}@(s\.whatsapp\.net|lid)$/.test(key.remoteJid) || !id(key.id)) return null;
  const sender = key.participant || key.remoteJid;
  try {
    const state = JSON.parse(readBounded(path.join(home, 'state/whatsapp-temporary-replies.json'), MAX_STATE, true));
    if (!object(state) || state.version !== 1 || !Array.isArray(state.grants) || state.grants.length > 32) return null;
    const seenIds = new Set();
    const seenAliases = new Set();
    for (const g of state.grants) {
      if (!object(g) || !id(g.id) || !text(g.name, 128) || !text(g.topic, 512) ||
          typeof g.phone_jid !== 'string' || !/^\d{5,15}@s\.whatsapp\.net$/.test(g.phone_jid) ||
          typeof g.lid_jid !== 'string' || !/^\d{5,20}@lid$/.test(g.lid_jid) ||
          typeof g.origin_chat_id !== 'string' || !/^\d{5,30}@g\.us$/.test(g.origin_chat_id) ||
          !Number.isSafeInteger(g.created_at) || !Number.isSafeInteger(g.expires_at) ||
          g.created_at < 0 || g.expires_at <= g.created_at || g.expires_at - g.created_at > WEEK ||
          !Array.isArray(g.delivery_message_ids) || g.delivery_message_ids.length < 1 ||
          g.delivery_message_ids.length > 32 || !g.delivery_message_ids.every(id) ||
          seenIds.has(g.id) || seenAliases.has(g.phone_jid) || seenAliases.has(g.lid_jid)) return null;
      seenIds.add(g.id); seenAliases.add(g.phone_jid); seenAliases.add(g.lid_jid);
    }
    const g = state.grants.find(g => [g.phone_jid, g.lid_jid].includes(key.remoteJid) &&
      [g.phone_jid, g.lid_jid].includes(sender) && g.created_at <= now && now < g.expires_at);
    if (!g) return null;
    const phone = g.phone_jid.split('@')[0];
    const lid = g.lid_jid.split('@')[0];
    const mapped = JSON.parse(readBounded(path.join(sessionDir, `lid-mapping-${phone}.json`), 1024));
    if (mapped !== lid && mapped !== g.lid_jid) return null;
    // The private grant file is operator authority; arming binds real delivery receipts.
    return g;
  } catch { return null; }
}

export function captureTemporaryReply(msg, grant, home = temporaryReplyHome(), now = Date.now()) {
  const content = getMessageContent(msg);
  const media = ['image', 'video', 'audio', 'document', 'sticker'].find(type => object(content[`${type}Message`]));
  const body = content.conversation ?? content.extendedTextMessage?.text ??
    (media ? content[`${media}Message`].caption : '') ?? '';
  if (typeof body !== 'string' || Buffer.byteLength(body) > 64 * 1024) throw new Error('invalid_body');
  const mediaNames = { image: 'imagem', video: 'vídeo', audio: 'áudio', document: 'documento', sticker: 'figurinha' };
  const notice = media ? `[Anexo recebido: ${mediaNames[media]}; arquivo não baixado automaticamente]` : '';
  const record = {
    contact: grant.name, grant_id: grant.id, received_at: new Date(now).toISOString(),
    message_id: msg.key.id, chat_id: msg.key.remoteJid,
    sender_id: msg.key.participant || msg.key.remoteJid,
    body: [body, notice].filter(Boolean).join('\n') || '[Mensagem não suportada]',
    media_type: media || '', media_urls: [], quoted_text: '', quoted_message_id: '', quoted_participant: '',
    origin_chat_id: grant.origin_chat_id, topic: grant.topic,
    delivery_message_ids: grant.delivery_message_ids, untrusted: true,
  };
  const stateDir = path.join(home, 'state');
  const inbox = path.join(stateDir, 'whatsapp-monitor-only-inbox.jsonl');
  mkdirSync(stateDir, { recursive: true, mode: 0o700 });
  // ponytail: single bounded O_APPEND write shares this inode with existing intake.
  // No rotation: the notifier owns a line cursor. Operator archival is needed at 8 MiB.
  const fd = openSync(inbox, constants.O_RDWR | constants.O_APPEND | constants.O_CREAT | constants.O_NOFOLLOW, 0o600);
  try {
    const stat = fstatSync(fd);
    if (!stat.isFile() || (stat.mode & 0o077) || (process.getuid && stat.uid !== process.getuid()) ||
        stat.size > MAX_INBOX) throw new Error('unsafe_inbox');
    const previous = Buffer.alloc(stat.size);
    if (readSync(fd, previous, 0, previous.length, 0) !== previous.length) throw new Error('short_read');
    const raw = previous.toString('utf8');
    if (raw && !raw.endsWith('\n')) throw new Error('incomplete_inbox');
    const rows = raw.split('\n').filter(Boolean).map(line => JSON.parse(line));
    if (rows.some(r => object(r) && r.grant_id === record.grant_id &&
        r.message_id === record.message_id && r.chat_id === record.chat_id)) return;
    const line = Buffer.from(JSON.stringify(record) + '\n');
    if (fstatSync(fd).size + line.length > MAX_INBOX) throw new Error('inbox_full');
    if (writeSync(fd, line) !== line.length) throw new Error('short_write');
    fsyncSync(fd);
    const dir = openSync(stateDir, 'r');
    try { fsyncSync(dir); } finally { closeSync(dir); }
    // Another writer may append concurrently; read back the exact complete record.
    if (!readBounded(inbox, MAX_INBOX, true).split('\n').includes(line.toString('utf8').trimEnd())) {
      throw new Error('readback_failed');
    }
  } finally { closeSync(fd); }
}
