import test from 'node:test';
import assert from 'node:assert/strict';
import os from 'node:os';
import path from 'node:path';
import { mkdtempSync, rmSync, writeFileSync, readFileSync } from 'node:fs';

import { recordReadOnlyReplyWatch } from './reply_watch.js';

function writeWatches(file, watches) {
  writeFileSync(file, JSON.stringify({ schema_version: 1, watches }, null, 2));
}

test('records a new exact-DM text reply without granting gateway access', () => {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'hermes-reply-watch-'));
  const statePath = path.join(dir, 'watches.json');
  try {
    writeWatches(statePath, [{
      id: 'jin-order', enabled: true,
      chat_jid: '8618320046787@s.whatsapp.net',
      participant_jids: ['8618320046787@s.whatsapp.net', '24936966037660@lid'],
      expires_at: '2030-01-01T00:00:00.000Z',
    }]);
    const result = recordReadOnlyReplyWatch({
      statePath,
      chatJid: '8618320046787@s.whatsapp.net',
      senderJid: '24936966037660@lid',
      messageId: 'MSG-1',
      timestampMs: 1700000000000,
      body: 'Tenho as seis disponíveis.',
      isGroup: false,
      fromMe: false,
      nowMs: 1700000000000,
    });
    assert.deepEqual(result, { recorded: true, watchId: 'jin-order' });
    const saved = JSON.parse(readFileSync(statePath, 'utf8'));
    assert.equal(saved.watches[0].events.length, 1);
    assert.equal(saved.watches[0].events[0].body, 'Tenho as seis disponíveis.');
    assert.equal(saved.watches[0].events[0].message_id, 'MSG-1');

    const aliasResult = recordReadOnlyReplyWatch({
      statePath,
      chatJid: '24936966037660@lid',
      senderJid: '24936966037660@lid',
      messageId: 'MSG-2',
      timestampMs: 1700000001000,
      body: 'Também respondi por LID.',
      isGroup: false,
      fromMe: false,
      nowMs: 1700000001000,
    });
    assert.deepEqual(aliasResult, { recorded: true, watchId: 'jin-order' });
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('never records group, self, expired, unmatched, or duplicate messages', () => {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'hermes-reply-watch-'));
  const statePath = path.join(dir, 'watches.json');
  try {
    writeWatches(statePath, [{
      id: 'jin-order', enabled: true,
      chat_jid: '8618320046787@s.whatsapp.net',
      participant_jids: ['8618320046787@s.whatsapp.net', '24936966037660@lid'],
      expires_at: '2030-01-01T00:00:00.000Z',
      events: [{ message_id: 'DUP' }],
    }]);
    const base = {
      statePath, chatJid: '8618320046787@s.whatsapp.net', senderJid: '24936966037660@lid',
      messageId: 'DUP', timestampMs: 1700000000000, body: 'x', isGroup: false, fromMe: false, nowMs: 1700000000000,
    };
    assert.deepEqual(recordReadOnlyReplyWatch(base), { recorded: false });
    assert.deepEqual(recordReadOnlyReplyWatch({ ...base, messageId: 'GROUP', isGroup: true }), { recorded: false });
    assert.deepEqual(recordReadOnlyReplyWatch({ ...base, messageId: 'SELF', fromMe: true }), { recorded: false });
    assert.deepEqual(recordReadOnlyReplyWatch({ ...base, messageId: 'OTHER', chatJid: '5511888888888@s.whatsapp.net' }), { recorded: false });
    const saved = JSON.parse(readFileSync(statePath, 'utf8'));
    assert.equal(saved.watches[0].events.length, 1);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
