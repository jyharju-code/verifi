import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  agentStatus, publicView, admissionRoute, isWallet,
  claimDigest, callbackHost, outcomeFromStatus,
} from './verify.js';

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

const quota = {
  pending_count: 0,
  queue_full: false,
  has_entry_entitlement: false,
  entitlement_admission_available: false,
};

test('a full queue still reaches the payment gate so the caller sees 402', () => {
  // Regression: answering 503 before x402 hid the payment requirements from
  // every unpaid caller, including discovery crawlers, whenever humans were
  // busy. The core rejects the full queue after the gate, and that rejection
  // cancels settlement, so the 0.10 USDC is never taken.
  assert.equal(admissionRoute({ ...quota, queue_full: true }), 'payment-gate');
});

test('a full queue does not consume an entitlement either', () => {
  assert.equal(
    admissionRoute({ ...quota, queue_full: true, entitlement_admission_available: true }),
    'entitlement',
  );
});

test('an active chain is rejected before the payment gate', () => {
  assert.equal(admissionRoute({ ...quota, pending_count: 1 }), 'active-chain');
  assert.equal(
    admissionRoute({ ...quota, pending_count: 1, entitlement_admission_available: true }),
    'active-chain',
  );
});

test('an entitlement is used only when it can actually be consumed', () => {
  assert.equal(admissionRoute({ ...quota, entitlement_admission_available: true }), 'entitlement');
  // Free allowance left but the platform's daily budget is spent: pay instead.
  assert.equal(
    admissionRoute({ ...quota, has_entry_entitlement: true, entitlement_admission_available: false }),
    'payment-gate',
  );
  // Older core without the budget field falls back to the plain entitlement.
  assert.equal(
    admissionRoute({ pending_count: 0, has_entry_entitlement: true }),
    'entitlement',
  );
});

test('only a wallet address identifies a quota', () => {
  assert.equal(isWallet('0x1111111111111111111111111111111111111111'), true);
  assert.equal(isWallet('0x111'), false);
  assert.equal(isWallet(undefined), false);
  assert.equal(isWallet(null), false);
  assert.equal(isWallet(12), false);
});

test('a claim is reduced to length and sha256, never its text, for the audit', () => {
  const claim = 'The launch date is 1 September and existing customers keep the old price.';
  const d = claimDigest(claim);
  assert.equal(d.claim_len, claim.length);
  assert.match(d.claim_sha256, /^[0-9a-f]{64}$/);
  // The digest must not carry the text or any substring of it.
  assert.ok(!JSON.stringify(d).includes('launch'));
  assert.ok(!JSON.stringify(d).includes(claim));
  // Empty or non-string claim yields nulls, not a hash of "".
  assert.deepEqual(claimDigest(''), { claim_len: null, claim_sha256: null });
  assert.deepEqual(claimDigest(undefined), { claim_len: null, claim_sha256: null });
});

test('a callback url is reduced to its host, dropping path and query', () => {
  assert.equal(
    callbackHost('https://agent.example.com/verifi-events?token=secret&id=9'),
    'agent.example.com',
  );
  assert.equal(callbackHost('not a url'), null);
  assert.equal(callbackHost(undefined), null);
  assert.equal(callbackHost(null), null);
});

test('outcome maps the money-relevant statuses', () => {
  assert.equal(outcomeFromStatus(202), 'admitted');
  assert.equal(outcomeFromStatus(402), 'payment_required');
  assert.equal(outcomeFromStatus(429), 'rejected_active');
  assert.equal(outcomeFromStatus(503), 'unavailable');
  assert.equal(outcomeFromStatus(200), 'completed');
  assert.equal(outcomeFromStatus(400), 'bad_request');
});

test('unlock ownership proof belongs to the current paid request', () => {
  const server = readFileSync(new URL('../app.js', import.meta.url), 'utf8');
  assert.match(
    server,
    /Looking up a paid verify proves nothing[\s\S]*wallet_ownership_proven = false/,
  );
  assert.match(
    server,
    /This handler runs only after the current unlock request[\s\S]*wallet_ownership_proven = true/,
  );
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
  assert.match(markdown, /every verification is paid/i);
  assert.match(html, /every verification is paid/i);
});
