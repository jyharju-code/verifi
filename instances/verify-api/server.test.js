/**
 * The process refuses to start without the core shared secret, so a missing
 * one shows up at deploy time instead of as failing verifies.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const dir = fileURLToPath(new URL('.', import.meta.url));

for (const secret of ['', 'too-short']) {
  test(`server.js exits when CORE_INTERNAL_SECRET is ${secret ? 'short' : 'empty'}`, () => {
    const run = spawnSync(process.execPath, ['server.js'], {
      cwd: dir,
      env: { ...process.env, CORE_INTERNAL_SECRET: secret, PORT: '0' },
      encoding: 'utf8',
      timeout: 20000,
    });
    assert.equal(run.status, 1);
    assert.match(run.stderr, /CORE_INTERNAL_SECRET must be set/);
  });
}
