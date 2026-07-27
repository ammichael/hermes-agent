import test from 'node:test';
import assert from 'node:assert/strict';
import os from 'node:os';
import path from 'node:path';
import { mkdtempSync, rmSync } from 'node:fs';

import {
  createTemporaryInteractionGrant,
  findTemporaryInteractionGrant,
} from './temporary_interaction.js';

test('grant is created only for a verified text delivery and matches exact DM scope', () => {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'hermes-wa-grant-'));
  const statePath = path.join(dir, 'grants.json');
  try {
    const grant = createTemporaryInteractionGrant({
      statePath,
      chatJid: '5511999999999@s.whatsapp.net',
      participantJids: ['123456789@lid'],
      topic: 'delivery follow-up',
      ttlSeconds: 3600,
      deliveryMessageIds: ['ABC123'],
    });
    assert.equal(grant.kind, 'temporary_interaction');
    assert.equal(findTemporaryInteractionGrant({
      statePath,
      chatJid: '5511999999999@s.whatsapp.net',
      senderJid: '123456789@lid',
      isGroup: false,
      isTextOnly: true,
    })?.id, grant.id);
    assert.equal(findTemporaryInteractionGrant({
      statePath,
      chatJid: '5511888888888@s.whatsapp.net',
      senderJid: '123456789@lid',
      isGroup: false,
      isTextOnly: true,
    }), null);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('failed delivery, media, groups, expiry, and missing state fail closed', () => {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'hermes-wa-grant-'));
  const statePath = path.join(dir, 'grants.json');
  try {
    assert.throws(() => createTemporaryInteractionGrant({
      statePath,
      chatJid: '5511999999999@s.whatsapp.net',
      participantJids: ['123456789@lid'],
      topic: 'delivery follow-up',
      ttlSeconds: 3600,
      deliveryMessageIds: [],
    }));
    assert.equal(findTemporaryInteractionGrant({
      statePath,
      chatJid: '5511999999999@s.whatsapp.net',
      senderJid: '123456789@lid',
      isGroup: false,
      isTextOnly: true,
    }), null);

    createTemporaryInteractionGrant({
      statePath,
      chatJid: '5511999999999@s.whatsapp.net',
      participantJids: ['123456789@lid'],
      topic: 'delivery follow-up',
      ttlSeconds: 1,
      deliveryMessageIds: ['ABC123'],
      nowMs: 1000,
    });
    const query = {
      statePath,
      chatJid: '5511999999999@s.whatsapp.net',
      senderJid: '123456789@lid',
      isGroup: false,
      isTextOnly: true,
      nowMs: 3000,
    };
    assert.equal(findTemporaryInteractionGrant(query), null);
    assert.equal(findTemporaryInteractionGrant({ ...query, nowMs: 1500, isGroup: true }), null);
    assert.equal(findTemporaryInteractionGrant({ ...query, nowMs: 1500, isTextOnly: false }), null);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
