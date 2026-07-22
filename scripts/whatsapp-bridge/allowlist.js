import path from 'path';
import { existsSync, readFileSync, statSync } from 'fs';
import os from 'os';

export function normalizeWhatsAppIdentifier(value) {
  return String(value || '')
    .trim()
    .replace(/:.*@/, '@')
    .replace(/@.*/, '')
    .replace(/^\+/, '');
}

export function parseAllowedUsers(rawValue) {
  return new Set(
    String(rawValue || '')
      .split(',')
      .map((value) => normalizeWhatsAppIdentifier(value))
      .filter(Boolean)
  );
}

function readMappingFile(sessionDir, identifier, suffix = '') {
  const filePath = path.join(sessionDir, `lid-mapping-${identifier}${suffix}.json`);
  if (!existsSync(filePath)) {
    return null;
  }

  try {
    const parsed = JSON.parse(readFileSync(filePath, 'utf8'));
    const normalized = normalizeWhatsAppIdentifier(parsed);
    return normalized || null;
  } catch {
    return null;
  }
}

export function expandWhatsAppIdentifiers(identifier, sessionDir) {
  const normalized = normalizeWhatsAppIdentifier(identifier);
  if (!normalized) {
    return new Set();
  }

  // Walk both phone->LID and LID->phone mapping files so allowlists can use
  // either form transparently in bot mode.
  const resolved = new Set();
  const queue = [normalized];

  while (queue.length > 0) {
    const current = queue.shift();
    if (!current || resolved.has(current)) {
      continue;
    }

    resolved.add(current);

    for (const suffix of ['', '_reverse']) {
      const mapped = readMappingFile(sessionDir, current, suffix);
      if (mapped && !resolved.has(mapped)) {
        queue.push(mapped);
      }
    }
  }

  return resolved;
}

/**
 * Temporary follow-up grants written by whatsapp-bridge-temp-allow.py.
 * Read on each match (mtime-cached) so outbound follow-ups can admit a
 * recipient without mutating WHATSAPP_ALLOWED_USERS or restarting the bridge
 * for every arm. Expired entries are ignored.
 *
 * Path: $HERMES_HOME/state/whatsapp-bridge-temp-allow.json
 *   or ~/.hermes/state/whatsapp-bridge-temp-allow.json
 *
 * Shape:
 * {
 *   "grants": [
 *     {
 *       "ids": ["5519...", "23377..."],
 *       "expires_at": "2026-07-22T11:22:39-03:00" | unix seconds,
 *       "name": "Renan Petronilho",
 *       "topic": "BuildersID..."
 *     }
 *   ]
 * }
 */
const TEMP_ALLOW_CACHE = {
  path: null,
  mtimeMs: null,
  checkedAtMs: 0,
  ids: new Set(),
};

function resolveTempAllowPath() {
  const hermesHome = process.env.HERMES_HOME
    || path.join(os.homedir(), '.hermes');
  return path.join(hermesHome, 'state', 'whatsapp-bridge-temp-allow.json');
}

function parseExpiry(value) {
  if (value == null || value === '') return null;
  if (typeof value === 'number' && Number.isFinite(value)) {
    // seconds vs ms
    return value > 1e12 ? value : value * 1000;
  }
  const ms = Date.parse(String(value));
  return Number.isFinite(ms) ? ms : null;
}

export function loadTemporaryAllowedIds(nowMs = Date.now(), options = {}) {
  const filePath = options.path || resolveTempAllowPath();
  const minRefreshMs = options.minRefreshMs ?? 1000;

  // Cheap cache: skip disk if we checked very recently and mtime is unchanged.
  if (
    TEMP_ALLOW_CACHE.path === filePath
    && (nowMs - TEMP_ALLOW_CACHE.checkedAtMs) < minRefreshMs
  ) {
    return TEMP_ALLOW_CACHE.ids;
  }

  TEMP_ALLOW_CACHE.path = filePath;
  TEMP_ALLOW_CACHE.checkedAtMs = nowMs;

  if (!existsSync(filePath)) {
    TEMP_ALLOW_CACHE.mtimeMs = null;
    TEMP_ALLOW_CACHE.ids = new Set();
    return TEMP_ALLOW_CACHE.ids;
  }

  let mtimeMs = null;
  try {
    mtimeMs = statSync(filePath).mtimeMs;
  } catch {
    TEMP_ALLOW_CACHE.ids = new Set();
    return TEMP_ALLOW_CACHE.ids;
  }

  if (TEMP_ALLOW_CACHE.mtimeMs === mtimeMs && TEMP_ALLOW_CACHE.ids) {
    // Still re-filter expiry without re-reading if we keep raw grants —
    // simpler to re-read small file when mtime changes only; on same mtime
    // re-evaluate expiry against now.
  }

  let raw;
  try {
    raw = JSON.parse(readFileSync(filePath, 'utf8'));
  } catch {
    TEMP_ALLOW_CACHE.mtimeMs = mtimeMs;
    TEMP_ALLOW_CACHE.ids = new Set();
    return TEMP_ALLOW_CACHE.ids;
  }

  TEMP_ALLOW_CACHE.mtimeMs = mtimeMs;
  const ids = new Set();
  const grants = Array.isArray(raw?.grants) ? raw.grants : [];
  for (const grant of grants) {
    if (!grant || grant.enabled === false) continue;
    const exp = parseExpiry(grant.expires_at);
    if (exp != null && nowMs > exp) continue;
    const list = Array.isArray(grant.ids) ? grant.ids : [];
    for (const id of list) {
      const n = normalizeWhatsAppIdentifier(id);
      if (n) ids.add(n);
    }
    // convenience single fields
    for (const key of ['jid', 'phone', 'lid']) {
      const n = normalizeWhatsAppIdentifier(grant[key]);
      if (n) ids.add(n);
    }
  }
  TEMP_ALLOW_CACHE.ids = ids;
  return ids;
}

export function matchesAllowedUser(senderId, allowedUsers, sessionDir) {
  // Empty allowlist = NO ONE allowed (secure default, #8389).  Operators
  // who want an open bot must set ``WHATSAPP_ALLOWED_USERS=*`` explicitly.
  // Previous behaviour (empty → return true) let any stranger DM the
  // bridge and trigger a Python-side pairing-code reply.
  const tempIds = loadTemporaryAllowedIds();
  const hasStatic = allowedUsers && allowedUsers.size > 0;
  const hasTemp = tempIds && tempIds.size > 0;
  if (!hasStatic && !hasTemp) {
    return false;
  }

  // "*" means allow everyone (consistent with SIGNAL_GROUP_ALLOWED_USERS)
  if (hasStatic && allowedUsers.has('*')) {
    return true;
  }

  const aliases = expandWhatsAppIdentifiers(senderId, sessionDir);
  for (const alias of aliases) {
    if (hasStatic && allowedUsers.has(alias)) {
      return true;
    }
    if (hasTemp && tempIds.has(alias)) {
      return true;
    }
  }

  return false;
}
