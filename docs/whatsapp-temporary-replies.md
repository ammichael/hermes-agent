# Capture-only WhatsApp reply contract (candidate, NOT activated)

Authority: `<HERMES_HOME>/state/whatsapp-temporary-replies.json`, regular owner-private file (0600; no symlink), maximum 256 KiB / 32 grants.

```json
{"version":1,"grants":[{"id":"vendor-window-1","name":"Fixture vendor","phone_jid":"15550000001@s.whatsapp.net","lid_jid":"10000000001@lid","created_at":1788700000000,"expires_at":1789304800000,"origin_chat_id":"100000000000@g.us","topic":"vendor quote","delivery_message_ids":["outbound-id"]}]}
```

Times are integer epoch milliseconds; duration must be positive and <= 604800000ms. IDs are ASCII `[A-Za-z0-9_-]{1,128}`. Parent arming must derive names, exact PN+LID, delivery IDs and creation time from verified outbound receipts and explicit user authorization; the private grant file is the trusted operator authority, not proof manufactured by inbound messages. The bridge independently checks the Baileys phone-to-LID mapping. Never add these contacts to permanent allowlists or enable wildcard/pairing access as part of activation.

Inbox: existing `<HERMES_HOME>/state/whatsapp-monitor-only-inbox.jsonl`. A single bounded O_APPEND write preserves existing writers/inode; fsync plus exact-record readback before returning captured. Existing records are preserved. Dedupe by grant/chat/message; one bridge writer. Inbox must be regular owner-private 0600; parent must migrate the existing owned 0644 inbox with chmod before activation. The 8 MiB ceiling fails closed; do not rotate/truncate without migrating the notifier's line cursor. A short write/storage failure requires operator repair; there is no automatic replay guarantee from Baileys.

Records match the existing notifier: `contact` (grant.name), `body`, `received_at` (ISO UTC), `message_id`, `chat_id`, `sender_id`, `media_urls:[]`, `media_type` (type or empty string), `quoted_text:''`, `quoted_message_id:''`, `quoted_participant:''`, plus `grant_id`, `origin_chat_id`, `topic`, `delivery_message_ids`, `untrusted:true`. Body/caption only, at most 64 KiB; media-only messages get a placeholder. No media download or arbitrary paths. Quoted content is deliberately not ingested.

Reuse existing `whatsapp-monitor-only-notify.py` and existing cron, no new scheduler. Its destination is deployment-fixed; `origin_chat_id` is correlation metadata, NOT routing authority. Parent MUST arm only grants whose origin equals that notifier destination. Other-origin support is not implemented.

Matching temporary messages stop before media extraction, message store, queue, Python authorization, sessions, hooks, commands, tools or automatic third-party answers. Capture failures also stop dispatch and emit a content-free error. Existing permanent allowlist and unrelated pairing behavior remain unchanged; activation requires these vendor identities absent from permanent trust and pairing/wildcard access disabled. Installation alone arms nothing. Activation and live inbound proof belong to the parent; no sends/restarts are performed by this candidate.
