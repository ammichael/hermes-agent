import { strict as assert } from 'node:assert';
import { createOutboundExposureGuard } from './outbound_exposure_guard.js';

let now = 1_000_000;
const sleeps = [];
const guard = createOutboundExposureGuard({
  now: () => now,
  sleep: async ms => { sleeps.push(ms); now += ms; },
  windowMs: 10_000,
  globalLimit: 4,
  perChatLimit: 2,
  duplicateTtlMs: 2_000,
  minimumSpacingMs: 100,
});

for (const text of [
  '⚡ Interrupting current task. I\'ll respond to your message shortly.',
  'Operation interrupted: waiting for model response (25.4s elapsed).',
  'Background process proc_deadbeef finished with exit code -15. Here\'s the final output:',
]) {
  await assert.rejects(
    () => guard.beforeSend('chat-a', { text }),
    error => error.code === 'operational_metadata_suppressed',
    'internal operational metadata never reaches WhatsApp',
  );
}

await guard.beforeSend('chat-a', { text: 'one' });
await guard.beforeSend('chat-a', { text: 'two' });
assert.deepStrictEqual(sleeps, [100], 'outbound sends are paced');

await assert.rejects(
  () => guard.beforeSend('chat-a', { text: 'three' }),
  error => error.code === 'chat_rate_limited',
  'per-chat bursts are capped',
);

await guard.beforeSend('chat-b', { text: 'one' });
await assert.rejects(
  () => guard.beforeSend('chat-b', { text: 'one' }),
  error => error.code === 'duplicate_suppressed',
  'exact duplicates are suppressed',
);

await guard.beforeSend('chat-c', { text: 'one' });
await assert.rejects(
  () => guard.beforeSend('chat-d', { text: 'one' }),
  error => error.code === 'global_rate_limited',
  'global bursts are capped',
);

now += 11_000;
await guard.beforeSend('chat-a', { text: 'one' });
assert.strictEqual(guard.snapshot().globalWindowCount, 1, 'window expires old sends');

console.log('outbound exposure guard: ok');
