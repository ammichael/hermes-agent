import makeWASocket, { useMultiFileAuthState } from '@whiskeysockets/baileys'
import P from 'pino'
import fs from 'node:fs'

const session='/Users/mike/.hermes/whatsapp/session'
const out='/Users/mike/.hermes/local-helper/N-profile-source.jpg'
const { state, saveCreds } = await useMultiFileAuthState(session)
const sock=makeWASocket({auth:state,logger:P({level:'silent'}),printQRInTerminal:false,syncFullHistory:false,markOnlineOnConnect:false})
sock.ev.on('creds.update', saveCreds)
let done=false
const finish=(code,msg)=>{if(done)return;done=true;console.log(msg);try{sock.end(undefined)}catch{};setTimeout(()=>process.exit(code),100)}
sock.ev.on('connection.update', async ({connection})=>{
  if(connection!=='open') return
  try {
    const jid=sock.user?.id
    if(!jid) throw new Error('own_jid_unavailable')
    const url=await sock.profilePictureUrl(jid,'image')
    const r=await fetch(url)
    if(!r.ok) throw new Error(`http_${r.status}`)
    const b=Buffer.from(await r.arrayBuffer())
    if(b.length<1024 || b.length>10_000_000) throw new Error('invalid_size')
    fs.writeFileSync(out,b,{mode:0o600})
    finish(0,`profile_picture=downloaded bytes=${b.length}`)
  } catch(e){ finish(2,`profile_picture=error code=${String(e.message).replace(/[^a-zA-Z0-9_-]/g,'_')}`) }
})
setTimeout(()=>finish(124,'profile_picture=timeout'),30000)
