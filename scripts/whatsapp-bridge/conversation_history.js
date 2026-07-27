import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import path from 'node:path';

const MAX_RECORDS_PER_CONVERSATION = 2000;

function normalizeJid(value) {
  const raw = String(value || '').trim().toLowerCase().replace(/:.*@/, '@');
  const id = raw.replace(/@.*/, '').replace(/^\+/, '');
  if (!id) return '';
  return `${id}${raw.endsWith('@lid') ? '@lid' : (raw.endsWith('@g.us') ? '@g.us' : '@s.whatsapp.net')}`;
}

function conversationPath(rootDir, conversationJid) {
  const digest = createHash('sha256').update(conversationJid).digest('hex').slice(0, 24);
  return path.join(rootDir, `${digest}.json`);
}

function load(file) {
  if (!existsSync(file)) return { schema_version: 1, conversation_jid: '', records: [] };
  try {
    const parsed = JSON.parse(readFileSync(file, 'utf8'));
    if (parsed?.schema_version === 1 && Array.isArray(parsed.records)) return parsed;
  } catch {}
  return { schema_version: 1, conversation_jid: '', records: [] };
}

function save(file, data) {
  mkdirSync(path.dirname(file), { recursive: true });
  const temp = `${file}.${process.pid}.tmp`;
  writeFileSync(temp, `${JSON.stringify(data, null, 2)}\n`, { encoding: 'utf8', mode: 0o600 });
  renameSync(temp, file);
}

export function appendConversationHistory({
  rootDir,
  chatJid,
  conversationJid,
  senderJid,
  messageId,
  timestampMs,
  fromMe,
  isGroup,
  body,
}) {
  const canonical = normalizeJid(conversationJid || chatJid);
  const text = String(body || '').trim();
  if (!rootDir || isGroup || !canonical || !messageId || !text) return { recorded: false };

  const file = conversationPath(rootDir, canonical);
  const data = load(file);
  if (data.records.some((record) => record.message_id === String(messageId))) return { recorded: false };

  const record = {
    message_id: String(messageId),
    chat_jid: normalizeJid(chatJid),
    sender_jid: normalizeJid(senderJid),
    received_at: new Date(Number.isFinite(timestampMs) ? timestampMs : Date.now()).toISOString(),
    from_me: !!fromMe,
    body: text.slice(0, 3500),
  };
  data.conversation_jid = canonical;
  data.records = [...data.records, record].slice(-MAX_RECORDS_PER_CONVERSATION);
  save(file, data);
  return { recorded: true, path: file };
}

export function readConversationHistory({ rootDir, chatJid }) {
  const canonical = normalizeJid(chatJid);
  if (!rootDir || !canonical) return [];
  const data = load(conversationPath(rootDir, canonical));
  return data.conversation_jid === canonical ? data.records : [];
}
