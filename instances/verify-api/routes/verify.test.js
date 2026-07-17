import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { agentStatus, publicView } from './verify.js';

const base = {
  id: '11111111-1111-4111-8111-111111111111',
  status: 'pending',
  verdict: null,
  explanation: null,
  response: null,
  response_time_ms: null,
  agent_id: '0x1111111111111111111111111111111111111111',
  entry_source: 'x402',
  entry_list_price_usdc: '0.10',
  entry_charged_usdc: '0.10',
  unlock_source: null,
  unlock_list_price_usdc: '2.90',
  unlock_charged_usdc: '0.00',
  result_unlocked: false,
  free_use_number: null,
  failure_credit_granted: false,
  created_at: '2026-07-17T10:00:00Z',
  admitted_at: '2026-07-17T10:00:01Z',
  expires_at: '2026-07-17T11:00:00Z',
  responded_at: null,
  unlocked_at: null,
};

test('admission and human processing map to a pollable processing state', () => {
  assert.equal(agentStatus({ ...base, status: 'admission_pending' }), 'processing');
  const view = publicView(base);
  assert.equal(view.status, 'processing');
  assert.equal(view.next_action, 'poll');
  assert.equal(view.retry_after_seconds, 15);
});

test('a ready paid result is locked behind the separate 2.90 USDC gate', () => {
  const view = publicView({
    ...base,
    status: 'refined',
    verdict: 'refined',
    explanation: 'Use the corrected value.',
    response: 'Use the corrected value.',
  });
  assert.equal(view.status, 'ready');
  assert.equal(view.verdict, null);
  assert.equal(view.unlock.payment_required, true);
  assert.equal(view.unlock.price_usdc, '2.90');
  assert.equal(view.funding.total_charged_usdc, '0.10');
});

test('one initial free entitlement covers both explicit gates', () => {
  const ready = publicView({
    ...base,
    status: 'accepted',
    entry_source: 'initial_free',
    entry_charged_usdc: '0.00',
    free_use_number: 3,
  });
  assert.equal(ready.status, 'ready');
  assert.equal(ready.unlock.payment_required, false);
  assert.equal(ready.unlock.price_usdc, '0.00');
  assert.equal(ready.funding.total_list_price_usdc, '3.00');
  assert.equal(ready.funding.total_charged_usdc, '0.00');
});

test('successful paid unlock releases the result only after the second gate', () => {
  const view = publicView({
    ...base,
    status: 'refined',
    verdict: 'refined',
    explanation: 'Use the corrected value.',
    response: 'Use the corrected value.',
    result_unlocked: true,
    unlock_source: 'x402',
    unlock_charged_usdc: '2.90',
  });
  assert.equal(view.status, 'completed');
  assert.equal(view.verdict, 'refined');
  assert.equal(view.explanation, 'Use the corrected value.');
  assert.equal(view.funding.total_charged_usdc, '3.00');
});

test('failed work stops polling and exposes the entry credit', () => {
  const view = publicView({
    ...base,
    status: 'expired',
    failure_reason: 'human_timeout',
    failure_credit_granted: true,
  });
  assert.equal(view.status, 'failed');
  assert.equal(view.next_action, 'stop');
  assert.equal(view.failure.entry_credit_granted, true);
  assert.equal(view.failure.entry_credit_value_usdc, '0.10');
});

test('code and rendered docs match the canonical contract', () => {
  const contract = JSON.parse(
    readFileSync(new URL('../../../docs/api-contract.json', import.meta.url), 'utf8'),
  );
  const markdown = readFileSync(new URL('../../../docs/API.md', import.meta.url), 'utf8');
  const html = readFileSync(
    new URL('../../../deploy/nginx/html/docs/index.html', import.meta.url),
    'utf8',
  );
  const view = publicView(base);
  assert.equal(contract.entryPrice, view.funding.entry_list_price_usdc);
  assert.equal(contract.unlockPrice, view.funding.unlock_list_price_usdc);
  assert.equal(contract.totalPrice, view.funding.total_list_price_usdc);
  assert.match(markdown, new RegExp(`Contract version: ${contract.contractVersion}`));
  assert.match(html, new RegExp(`data-contract-version="${contract.contractVersion}"`));
  assert.match(markdown, /five complete free chains/i);
  assert.match(html, /Five complete free chains per wallet/i);
});
