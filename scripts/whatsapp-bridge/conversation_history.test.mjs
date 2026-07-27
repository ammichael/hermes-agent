import assert from 'node:assert/strict';
import test from 'node:test';
import os from 'node:os';
import path from 'node:path';
import { mkdtempSync, rmSync } from 'node:fs';

import { appendConversationHistory, readConversationHistory } from './conversation_history.js';

test('retains inbound and outbound DM text across bridge restarts', () => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'hermes-wa-history-'));
  try {
    const first = appendConversationHistory({
      rootDir: root,
      chatJid: '8618320046787@s.whatsapp.net',
      senderJid: '24936966037660@lid',
      messageId: 'IN-1',
      timestampMs: 1700000000000,
      fromMe: false,
      isGroup: false,
      body: 'Tenho França e Espanha.',
    });
    assert.equal(first.recorded, true);

    const second = appendConversationHistory({
      rootDir: root,
      chatJid: '24936966037660@lid',
      conversationJid: '8618320046787@s.whatsapp.net',
      senderJid: '5511981524102@s.whatsapp.net',
      messageId: 'OUT-1',
      timestampMs: 1700000001000,
      fromMe: true,
      isGroup: false,
      body: 'Pode confirmar a Argentina?',
    });
    assert.equal(second.recorded, true);

    const duplicate = appendConversationHistory({
      rootDir: root,
      chatJid: '8618320046787@s.whatsapp.net',
      senderJid: '24936966037660@lid',
      messageId: 'IN-1',
      timestampMs: 1700000000000,
      fromMe: false,
      isGroup: false,
      body: 'Tenho França e Espanha.',
    });
    assert.equal(duplicate.recorded, false);

    const records = readConversationHistory({ rootDir: root, chatJid: '8618320046787@s.whatsapp.net' });
    assert.deepEqual(records.map((record) => [record.message_id, record.from_me, record.body]), [
      ['IN-1', false, 'Tenho França e Espanha.'],
      ['OUT-1', true, 'Pode confirmar a Argentina?'],
    ]);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test('does not persist empty or group messages into a DM conversation journal', () => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'hermes-wa-history-'));
  try {
    assert.deepEqual(appendConversationHistory({ rootDir: root, chatJid: '120@g.us', messageId: 'GROUP', isGroup: true, body: 'x' }), { recorded: false });
    assert.deepEqual(appendConversationHistory({ rootDir: root, chatJid: '8618320046787@s.whatsapp.net', messageId: 'EMPTY', isGroup: false, body: '' }), { recorded: false });
    assert.deepEqual(readConversationHistory({ rootDir: root, chatJid: '8618320046787@s.whatsapp.net' }), []);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
