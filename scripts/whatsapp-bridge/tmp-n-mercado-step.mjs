import makeWASocket, { useMultiFileAuthState } from '@whiskeysockets/baileys'
import P from 'pino'

const action = process.argv[2]
const subject = 'N / Mercado'
const avisosJid = '120363409702814784@g.us'
const { state, saveCreds } = await useMultiFileAuthState('/Users/mike/.hermes/whatsapp/session')
const sock = makeWASocket({ auth: state, logger: P({ level: 'silent' }), printQRInTerminal: false, browser: ['Hermes Group Step', 'Chrome', '1.0'], syncFullHistory: false, markOnlineOnConnect: false })
sock.ev.on('creds.update', saveCreds)
await new Promise((resolve, reject) => {
  const timer = setTimeout(() => reject(new Error('connection timeout')), 45000)
  sock.ev.on('connection.update', update => {
    if (update.connection === 'open') { clearTimeout(timer); resolve() }
    if (update.connection === 'close' && update.lastDisconnect?.error) { clearTimeout(timer); reject(update.lastDisconnect.error) }
  })
})
try {
  if (action === 'find') {
    const all = await sock.groupFetchAllParticipating()
    const exact = Object.values(all).filter(g => g.subject === subject)
    console.log(JSON.stringify({ count: exact.length, groupJid: exact[0]?.id || null, participantCount: exact[0]?.participants?.length || null, adminCount: exact[0]?.participants?.filter(p => p.admin).length || null }))
  } else if (action === 'community') {
    const avisos = await sock.groupMetadata(avisosJid)
    console.log(JSON.stringify({ communityJid: avisos.linkedParent || null, avisosSubject: avisos.subject }))
  } else if (action === 'promote') {
    const groupJid = process.env.GROUP_JID
    const meta = await sock.groupMetadata(groupJid)
    const nonAdmins = meta.participants.filter(p => !p.admin)
    if (nonAdmins.length === 1 && meta.participants.length === 2) await sock.groupParticipantsUpdate(groupJid, [nonAdmins[0].id], 'promote')
    console.log(JSON.stringify({ promoted: nonAdmins.length === 1 && meta.participants.length === 2 }))
  } else if (action === 'link') {
    await sock.communityLinkGroup(process.env.GROUP_JID, process.env.COMMUNITY_JID)
    console.log(JSON.stringify({ linkRequested: true }))
  } else if (action === 'verify') {
    const linked = await sock.communityFetchLinkedGroups(process.env.COMMUNITY_JID)
    const found = linked.linkedGroups.find(g => g.id === process.env.GROUP_JID)
    console.log(JSON.stringify({ linked: Boolean(found), subject: found?.subject || null, size: found?.size || null }))
  } else if (action === 'metadata') {
    const meta = await sock.groupMetadata(process.env.GROUP_JID)
    console.log(JSON.stringify({ subject: meta.subject, participantCount: meta.participants.length, adminCount: meta.participants.filter(p => p.admin).length, linkedParent: meta.linkedParent || null }))
  } else throw new Error(`unknown action ${action}`)
} finally {
  try { sock.ws?.close() } catch {}
}
