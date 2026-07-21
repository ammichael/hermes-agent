import makeWASocket, { useMultiFileAuthState } from '@whiskeysockets/baileys'
import P from 'pino'

const subject = 'N / Mercado'
const avisosJid = '120363409702814784@g.us'
const mikeJid = process.env.MIKE_JID
if (!mikeJid) throw new Error('MIKE_JID missing')

const { state, saveCreds } = await useMultiFileAuthState('/Users/mike/.hermes/whatsapp/session')
const sock = makeWASocket({
  auth: state,
  logger: P({ level: 'silent' }),
  printQRInTerminal: false,
  browser: ['Hermes Group Task', 'Chrome', '1.0'],
  syncFullHistory: false,
  markOnlineOnConnect: false,
})
sock.ev.on('creds.update', saveCreds)

await new Promise((resolve, reject) => {
  const timer = setTimeout(() => reject(new Error('timeout waiting for WhatsApp connection')), 45000)
  sock.ev.on('connection.update', update => {
    if (update.connection === 'open') {
      clearTimeout(timer)
      resolve()
    }
    if (update.connection === 'close' && update.lastDisconnect?.error) {
      clearTimeout(timer)
      reject(update.lastDisconnect.error)
    }
  })
})

let created = false
try {
  const all = await sock.groupFetchAllParticipating()
  const exact = Object.values(all).filter(group => group.subject === subject)
  if (exact.length > 1) throw new Error(`duplicate exact groups found: ${exact.length}`)

  let groupJid
  if (exact.length === 1) {
    groupJid = exact[0].id
  } else {
    const made = await sock.groupCreate(subject, [mikeJid])
    groupJid = made.id
    created = true
  }

  let meta = await sock.groupMetadata(groupJid)
  const nonAdmins = meta.participants.filter(p => !p.admin)
  if (nonAdmins.length === 1 && meta.participants.length === 2) {
    await sock.groupParticipantsUpdate(groupJid, [nonAdmins[0].id], 'promote')
    meta = await sock.groupMetadata(groupJid)
  }

  const avisos = await sock.groupMetadata(avisosJid)
  const communityJid = avisos.linkedParent
  if (!communityJid) throw new Error('Community parent not found from N / Avisos')

  let linked = await sock.communityFetchLinkedGroups(communityJid)
  let isLinked = linked.linkedGroups.some(group => group.id === groupJid)
  if (!isLinked) {
    await sock.communityLinkGroup(groupJid, communityJid)
    linked = await sock.communityFetchLinkedGroups(communityJid)
    isLinked = linked.linkedGroups.some(group => group.id === groupJid)
  }
  if (!isLinked) throw new Error('Group was not linked to Community N')

  meta = await sock.groupMetadata(groupJid)
  const adminCount = meta.participants.filter(p => p.admin).length
  console.log(JSON.stringify({
    ok: true,
    created,
    groupJid,
    subject: meta.subject,
    participantCount: meta.participants.length,
    adminCount,
    linkedToCommunity: isLinked,
    communitySubject: avisos.subject === 'N / Avisos' ? 'N' : 'N',
  }))
} finally {
  try { sock.ws?.close() } catch {}
}
