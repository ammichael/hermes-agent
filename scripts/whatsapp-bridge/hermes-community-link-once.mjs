import makeWASocket, { DisconnectReason, useMultiFileAuthState } from '@whiskeysockets/baileys'
import P from 'pino'

const sessionPath = '/Users/mike/.hermes/whatsapp/session'
const targetSubject = process.argv[2]
if (!targetSubject || !/^N \/ [^\r\n]{1,80}$/.test(targetSubject)) {
  console.error('invalid_target_subject')
  process.exit(64)
}
const { state, saveCreds } = await useMultiFileAuthState(sessionPath)
const sock = makeWASocket({
  auth: state,
  logger: P({ level: 'silent' }),
  printQRInTerminal: false,
  browser: ['N', 'Chrome', '1.0'],
  syncFullHistory: false,
  markOnlineOnConnect: false,
})
sock.ev.on('creds.update', saveCreds)
await new Promise((resolve, reject) => {
  const timer = setTimeout(() => reject(new Error('connection_timeout')), 45000)
  sock.ev.on('connection.update', update => {
    if (update.connection === 'open') { clearTimeout(timer); resolve() }
    if (update.connection === 'close') {
      const code = update.lastDisconnect?.error?.output?.statusCode
      if (code !== DisconnectReason.restartRequired) {
        clearTimeout(timer)
        reject(new Error('connection_closed'))
      }
    }
  })
})
const groups = await sock.groupFetchAllParticipating()
const all = Object.values(groups)
const communityCandidates = all.filter(g => g.subject === 'N' && g.id?.endsWith('@g.us') && g.isCommunity === true && g.isCommunityAnnounce !== true)
const homeAnnouncementJid = '120363409702814784@g.us'
let community = null
for (const candidate of communityCandidates) {
  try {
    const candidateLinked = await sock.communityFetchLinkedGroups(candidate.id)
    if ((candidateLinked?.linkedGroups ?? []).some(g => g.id === homeAnnouncementJid)) {
      community = candidate
      break
    }
  } catch {}
}
const target = all.find(g => g.subject === targetSubject && g.id?.endsWith('@g.us'))
if (!community) throw new Error('community_not_found')
if (!target) throw new Error('target_group_not_found')
if (process.env.WRITE_TARGET_JID_FILE === '1') {
  const fs = await import('node:fs')
  fs.writeFileSync('/tmp/n-companion-jid', `${target.id}\n`, { mode: 0o600 })
  console.log(JSON.stringify({ target_found: true }))
  try { sock.ws?.close() } catch {}
  process.exit(0)
}
let linked = await sock.communityFetchLinkedGroups(community.id)
let items = linked?.linkedGroups ?? []
const already = items.some(g => g.id === target.id)
if (!already) await sock.communityLinkGroup(target.id, community.id)
linked = await sock.communityFetchLinkedGroups(community.id)
items = linked?.linkedGroups ?? []
const verified = items.some(g => g.id === target.id)
console.log(JSON.stringify({ community_found: true, target_found: true, already_linked: already, linked_verified: verified, linked_group_count: items.length }))
try { sock.ws?.close() } catch {}
process.exit(verified ? 0 : 2)
