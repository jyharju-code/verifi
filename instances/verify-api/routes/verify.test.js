import test from 'node:test';
import assert from 'node:assert/strict';

import { effectiveWaitSeconds } from './verify.js';

test('paid verifies return immediately even when a synchronous wait was requested', () => {
  assert.equal(effectiveWaitSeconds('paid', 110), 0);
  assert.equal(effectiveWaitSeconds('paid', 30), 0);
});

test('free verifies keep the requested synchronous wait', () => {
  assert.equal(effectiveWaitSeconds('free', 110), 110);
  assert.equal(effectiveWaitSeconds('free', 0), 0);
});
