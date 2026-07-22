import test from 'node:test';
import assert from 'node:assert/strict';
import os from 'node:os';
import path from 'node:path';
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';

import {
  expandWhatsAppIdentifiers,
  matchesAllowedUser,
  normalizeWhatsAppIdentifier,
  parseAllowedUsers,
} from './allowlist.js';

test('normalizeWhatsAppIdentifier strips jid syntax and plus prefix', () => {
  assert.equal(normalizeWhatsAppIdentifier('+19175395595@s.whatsapp.net'), '19175395595');
  assert.equal(normalizeWhatsAppIdentifier('267383306489914@lid'), '267383306489914');
  assert.equal(normalizeWhatsAppIdentifier('19175395595:12@s.whatsapp.net'), '19175395595');
});

test('expandWhatsAppIdentifiers resolves phone and lid aliases from session files', () => {
  const sessionDir = mkdtempSync(path.join(os.tmpdir(), 'hermes-wa-allowlist-'));

  try {
    writeFileSync(path.join(sessionDir, 'lid-mapping-19175395595.json'), JSON.stringify('267383306489914'));
    writeFileSync(path.join(sessionDir, 'lid-mapping-267383306489914_reverse.json'), JSON.stringify('19175395595'));

    const aliases = expandWhatsAppIdentifiers('267383306489914@lid', sessionDir);
    assert.deepEqual([...aliases].sort(), ['19175395595', '267383306489914']);
  } finally {
    rmSync(sessionDir, { recursive: true, force: true });
  }
});

test('matchesAllowedUser accepts mapped lid sender when allowlist only contains phone number', () => {
  const sessionDir = mkdtempSync(path.join(os.tmpdir(), 'hermes-wa-allowlist-'));
  const hermesHome = mkdtempSync(path.join(os.tmpdir(), 'hermes-home-allow-'));
  const prevHome = process.env.HERMES_HOME;
  process.env.HERMES_HOME = hermesHome;

  try {
    writeFileSync(path.join(sessionDir, 'lid-mapping-19175395595.json'), JSON.stringify('267383306489914'));
    writeFileSync(path.join(sessionDir, 'lid-mapping-267383306489914_reverse.json'), JSON.stringify('19175395595'));

    const allowedUsers = parseAllowedUsers('+19175395595');
    assert.equal(matchesAllowedUser('267383306489914@lid', allowedUsers, sessionDir), true);
    assert.equal(matchesAllowedUser('188012763865257@lid', allowedUsers, sessionDir), false);
  } finally {
    if (prevHome === undefined) delete process.env.HERMES_HOME;
    else process.env.HERMES_HOME = prevHome;
    rmSync(sessionDir, { recursive: true, force: true });
    rmSync(hermesHome, { recursive: true, force: true });
  }
});

test('matchesAllowedUser treats * as allow-all wildcard', () => {
  const sessionDir = mkdtempSync(path.join(os.tmpdir(), 'hermes-wa-allowlist-'));
  const hermesHome = mkdtempSync(path.join(os.tmpdir(), 'hermes-home-allow-'));
  const prevHome = process.env.HERMES_HOME;
  process.env.HERMES_HOME = hermesHome;

  try {
    const allowedUsers = parseAllowedUsers('*');
    assert.equal(matchesAllowedUser('19175395595@s.whatsapp.net', allowedUsers, sessionDir), true);
    assert.equal(matchesAllowedUser('267383306489914@lid', allowedUsers, sessionDir), true);
  } finally {
    if (prevHome === undefined) delete process.env.HERMES_HOME;
    else process.env.HERMES_HOME = prevHome;
    rmSync(sessionDir, { recursive: true, force: true });
    rmSync(hermesHome, { recursive: true, force: true });
  }
});

test('matchesAllowedUser rejects everyone when allowlist is empty (#8389)', () => {
  // Regression guard: empty allowlist used to return true (allow-everyone),
  // which let any stranger DM the bridge and trigger a Python-side
  // pairing-code reply. Secure default is now "reject unless explicitly
  // configured"; operators who want an open bot must set `*`.
  const sessionDir = mkdtempSync(path.join(os.tmpdir(), 'hermes-wa-allowlist-'));
  const hermesHome = mkdtempSync(path.join(os.tmpdir(), 'hermes-home-allow-'));
  const prevHome = process.env.HERMES_HOME;

  try {
    // Isolate from any real temp-allow grants on the machine.
    process.env.HERMES_HOME = hermesHome;

    const empty = parseAllowedUsers('');
    assert.equal(empty.size, 0);
    assert.equal(matchesAllowedUser('19175395595@s.whatsapp.net', empty, sessionDir), false);
    assert.equal(matchesAllowedUser('267383306489914@lid', empty, sessionDir), false);

    // Null/undefined allowlist (defensive) also rejects.
    assert.equal(matchesAllowedUser('19175395595@s.whatsapp.net', null, sessionDir), false);
    assert.equal(matchesAllowedUser('19175395595@s.whatsapp.net', undefined, sessionDir), false);
  } finally {
    if (prevHome === undefined) delete process.env.HERMES_HOME;
    else process.env.HERMES_HOME = prevHome;
    rmSync(sessionDir, { recursive: true, force: true });
    rmSync(hermesHome, { recursive: true, force: true });
  }
});

test('matchesAllowedUser admits temporary follow-up grants from state file', () => {
  const sessionDir = mkdtempSync(path.join(os.tmpdir(), 'hermes-wa-allowlist-'));
  const hermesHome = mkdtempSync(path.join(os.tmpdir(), 'hermes-home-allow-'));
  const prevHome = process.env.HERMES_HOME;
  process.env.HERMES_HOME = hermesHome;

  try {
    const stateDir = path.join(hermesHome, 'state');
    mkdirSync(stateDir, { recursive: true });
    const expires = new Date(Date.now() + 60 * 60 * 1000).toISOString();
    writeFileSync(
      path.join(stateDir, 'whatsapp-bridge-temp-allow.json'),
      JSON.stringify({
        grants: [
          {
            name: 'Renan',
            topic: 'BuildersID',
            ids: ['5519982339900', '233779398463494'],
            expires_at: expires,
          },
        ],
      }),
    );

    const empty = parseAllowedUsers('');
    assert.equal(matchesAllowedUser('5519982339900@s.whatsapp.net', empty, sessionDir), true);
    assert.equal(matchesAllowedUser('233779398463494@lid', empty, sessionDir), true);
    assert.equal(matchesAllowedUser('5519999999999@s.whatsapp.net', empty, sessionDir), false);
  } finally {
    if (prevHome === undefined) delete process.env.HERMES_HOME;
    else process.env.HERMES_HOME = prevHome;
    rmSync(sessionDir, { recursive: true, force: true });
    rmSync(hermesHome, { recursive: true, force: true });
  }
});
