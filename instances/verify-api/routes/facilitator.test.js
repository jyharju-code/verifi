import test from 'node:test';
import assert from 'node:assert/strict';

import { facilitatorConfig } from '../app.js';

test('a facilitator URL is used as it is', () => {
  assert.deepEqual(facilitatorConfig({}, 'http://facilitator:8080'), { url: 'http://facilitator:8080' });
});

test('cdp selects the Coinbase facilitator with authenticated requests', () => {
  const cfg = facilitatorConfig({ CDP_API_KEY_ID: 'id', CDP_API_KEY_SECRET: 'secret' }, 'cdp');
  assert.match(cfg.url, /^https:\/\/api\.cdp\.coinbase\.com\//);
  assert.equal(typeof cfg.createAuthHeaders, 'function');
});

test('cdp without keys refuses to start rather than settle unauthenticated', () => {
  assert.throws(() => facilitatorConfig({}, 'cdp'), /CDP_API_KEY_ID/);
});
