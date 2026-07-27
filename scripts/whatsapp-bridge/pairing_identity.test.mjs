import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const bridgePath = fileURLToPath(new URL('./bridge.js', import.meta.url));

test('uses the known-compatible unauthenticated pairing configuration', () => {
  const bridgeSource = readFileSync(bridgePath, 'utf8');

  assert.match(
    bridgeSource,
    /browser:\s*\['Hermes Agent', 'Chrome', '120\.0'\]/,
  );
  assert.match(bridgeSource, /syncFullHistory:\s*false/);
});
