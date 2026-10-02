/**
 * Two fixes on the paid paths, end to end with the real x402 middleware:
 * a credit is spent only by its wallet's owner, and a cold unlock probe sees
 * a real 402 that can never take money.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { x402Client } from '@x402/core/client';
import { ExactEvmScheme } from '@x402/evm/exact/client';
import { decodePaymentRequiredHeader, encodePaymentSignatureHeader } from '@x402/core/http';
import { generatePrivateKey, privateKeyToAccount } from 'viem/accounts';

import { USDC, EURC, NETWORK, account, quote, boundTerms, signed, withStack } from './testkit.js';

const settles = (facilitator) => facilitator.calls.filter((c) => c[0] === 'settle');

function verifyRequest(base, agentId, headers = {}) {
  return fetch(`${base}/verify`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', ...headers },
    body: JSON.stringify({ intent: 'Check before acting.', claim: 'The sky is green.', agent_id: agentId }),
  });
}

async function signAs(signer, paymentRequired) {
  const client = new x402Client().register('eip155:*', new ExactEvmScheme(signer));
  return client.createPaymentPayload(paymentRequired);
}

// ---------------------------------------------------------------- credits

test('a credited wallet gets the same 402 as everyone, not a free 202', () => withStack(async ({ core, facilitator, base }) => {
  core.credited.add(account.address.toLowerCase());
  const res = await verifyRequest(base, account.address);
  assert.equal(res.status, 402);
  assert.ok(res.headers.get('payment-required'));
  assert.equal(core.created.length, 0);
  assert.deepEqual(facilitator.calls, []);
}));

test('the owner signs the 402 and the credit is used: no funds move', () => withStack(async ({ core, facilitator, base }) => {
  core.credited.add(account.address.toLowerCase());
  const first = await verifyRequest(base, account.address);
  const payload = await signed(decodePaymentRequiredHeader(first.headers.get('payment-required')));
  const res = await verifyRequest(base, account.address, { 'PAYMENT-SIGNATURE': encodePaymentSignatureHeader(payload) });
  assert.equal(res.status, 202);
  assert.equal((await res.json()).funding.entry_source, 'failure_credit');
  assert.deepEqual(core.created.map((c) => c.admission_mode), ['entitlement']);
  // Neither verified nor settled: the authorization is never submitted.
  assert.deepEqual(facilitator.calls, []);
  assert.equal(res.headers.get('payment-response'), null);
}));

test("someone else cannot spend a wallet's credit by naming it", () => withStack(async ({ core, facilitator, base }) => {
  const victim = privateKeyToAccount(generatePrivateKey()).address;
  core.credited.add(victim.toLowerCase());
  const first = await verifyRequest(base, victim);
  const required = decodePaymentRequiredHeader(first.headers.get('payment-required'));
  // The attacker signs with their own key but names the victim's wallet.
  const payload = await signAs(account, required);
  const res = await verifyRequest(base, victim, { 'PAYMENT-SIGNATURE': encodePaymentSignatureHeader(payload) });
  // No proof for the victim: the request goes the paid way and the signer pays.
  assert.equal(res.status, 202);
  assert.deepEqual(core.created.map((c) => c.admission_mode), ['x402']);
  assert.equal(settles(facilitator).length, 1);
}));

test('a signature against an expired quote proves nothing and pays nothing', () => withStack(async ({ core, facilitator, base }) => {
  core.credited.add(account.address.toLowerCase());
  const first = await verifyRequest(base, account.address);
  const payload = await signed(decodePaymentRequiredHeader(first.headers.get('payment-required')));
  core.quotes.get('st_one').valid = false;
  core.publish(quote('st_two'));
  const res = await verifyRequest(base, account.address, { 'PAYMENT-SIGNATURE': encodePaymentSignatureHeader(payload) });
  assert.equal(res.status, 402);
  assert.equal(core.created.length, 0);
  assert.deepEqual(facilitator.calls, []);
}));

// ---------------------------------------------------------------- unlock band

const ID = '0f3f7d0a-0000-4000-8000-0000000000bb';

function unlock(base, query, headers = {}) {
  return fetch(`${base}/verify-unlock${query}`, { method: 'POST', headers });
}

test('a cold unlock probe sees a 402 with both unlock prices for every asset', () => withStack(async ({ facilitator, base }) => {
  for (const query of ['', '?id=not-a-uuid', `?id=${ID}`]) {
    const res = await unlock(base, query);
    assert.equal(res.status, 402, query);
    const required = decodePaymentRequiredHeader(res.headers.get('payment-required'));
    assert.deepEqual(required.accepts.map((a) => [a.asset, a.amount]), [
      [USDC, '3400000'], [USDC, '1700000'], [EURC, '2900000'], [EURC, '1450000'],
    ]);
    assert.equal(required.extensions['service-windows'].info.terms_id, 'st_one');
    assert.match((await res.json()).error, /verify id of a ready chain/);
  }
  assert.deepEqual(facilitator.calls, []);
}));

test('paying the band without a ready chain is refused and never settled', () => withStack(async ({ facilitator, base }) => {
  for (const [query, status] of [['', 400], [`?id=${ID}`, 404]]) {
    const first = await unlock(base, query);
    const payload = await signed(decodePaymentRequiredHeader(first.headers.get('payment-required')));
    const res = await unlock(base, query, { 'PAYMENT-SIGNATURE': encodePaymentSignatureHeader(payload) });
    assert.equal(res.status, status, query);
    assert.match((await res.json()).detail, /No payment was taken/);
    assert.equal(res.headers.get('payment-response'), null);
  }
  assert.equal(settles(facilitator).length, 0);
}));

test('a band signature cannot unlock a real chain', () => withStack(async ({ core, facilitator, base }) => {
  const probe = await unlock(base, '');
  const bandPayload = await signed(
    decodePaymentRequiredHeader(probe.headers.get('payment-required')),
    (_v, accepts) => accepts.find((a) => a.asset === EURC && a.amount === '1450000'),
  );
  const t = boundTerms(quote('st_one'), EURC);
  core.verifies.set(ID, {
    id: ID, status: 'accepted', result_unlocked: false, entry_source: 'x402', agent_id: account.address,
    contract_version: 3, terms_id: 'st_one', bound_terms: t, bound_asset: EURC, bound_network: NETWORK,
    entry_amount_atomic: t.admission, entry_charged_atomic: t.admission, admitted_at: '2026-10-01T09:00:00Z',
    sla_deadline: '2026-10-01T10:00:00Z', grace_deadline: '2026-10-02T09:00:00Z', ready_at: '2026-10-01T09:30:00Z',
    applied_window: 'sla', unlock_amount_atomic: t.sla, created_at: '2026-10-01T09:00:00Z',
  });
  // The chain was answered in the SLA window (2.90); a 1.45 band signature must not open it.
  const res = await unlock(base, `?id=${ID}`, { 'PAYMENT-SIGNATURE': encodePaymentSignatureHeader(bandPayload) });
  assert.equal(res.status, 402);
  assert.equal(settles(facilitator).length, 0);
  const required = decodePaymentRequiredHeader(res.headers.get('payment-required'));
  assert.deepEqual(required.accepts.map((a) => [a.asset, a.amount]), [[EURC, '2900000']]);
}));
