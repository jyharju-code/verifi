/**
 * Status and gates 2 and 3, in process, against a fake core and facilitator.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { decodePaymentRequiredHeader, encodePaymentSignatureHeader } from '@x402/core/http';

import { publicView } from './verify.js';
import { USDC, EURC, NETWORK, quote, boundTerms, signed, settleRecorded, withStack } from './testkit.js';

const ID = '0f3f7d0a-0000-4000-8000-0000000000aa';

function chain(asset, { status = 'accepted', window = 'sla', unlocked = false, credit = false } = {}) {
  const t = boundTerms(quote('st_one'), asset);
  const ready = ['accepted', 'rejected', 'refined'].includes(status);
  return {
    id: ID, verify_no: 7, status, agent_id: '0x1111111111111111111111111111111111111111',
    verdict: ready ? status : null, explanation: ready ? 'Checked by a human.' : null,
    response: ready ? 'Checked by a human.' : null, response_time_ms: ready ? 1234 : null,
    entry_source: 'x402', entry_list_price_usdc: '0.10', entry_charged_usdc: asset === USDC ? '0.12' : '0.00',
    unlock_source: unlocked ? 'x402' : null, unlock_list_price_usdc: '2.90', unlock_charged_usdc: '0.00',
    result_unlocked: unlocked, free_use_number: null, failure_credit_granted: credit,
    failure_reason: status === 'expired' ? 'human_timeout' : null,
    contract_version: 3, terms_id: 'st_one', bound_terms: t, bound_asset: asset, bound_network: NETWORK,
    entry_amount_atomic: t.admission, entry_charged_atomic: t.admission,
    created_at: '2026-10-01T09:00:00+00:00', admitted_at: '2026-10-01T09:00:02+00:00',
    sla_deadline: '2026-10-01T10:00:02+00:00', grace_deadline: '2026-10-02T09:00:02+00:00',
    expires_at: '2026-10-02T09:00:02+00:00',
    ready_at: ready ? (window === 'sla' ? '2026-10-01T09:40:00+00:00' : '2026-10-01T13:12:40+00:00') : null,
    responded_at: ready ? '2026-10-01T09:40:00+00:00' : null,
    applied_window: ready ? window : null,
    unlock_amount_atomic: ready ? t[window] : null,
    unlock_charged_atomic: unlocked ? t[window] : null,
    unlocked_at: null,
  };
}

test('a ready EURC chain shows its window and price, and never a USDC number', () => {
  const view = publicView(chain(EURC, { window: 'grace' }));
  assert.equal(view.status, 'ready');
  assert.equal(view.work_id, ID);
  assert.equal(view.contract_version, 3);
  assert.equal(view.verdict, null);
  assert.deepEqual(view.service_window, {
    applied: 'grace', sla_deadline: '2026-10-01T10:00:02+00:00', grace_deadline: '2026-10-02T09:00:02+00:00',
    network: NETWORK, asset: EURC, unlock_amount: '1450000',
  });
  assert.equal(view.unlock_url, `https://verifi.cloud/verify-unlock?id=${ID}`);
  assert.equal(view.unlock.url, `/verify-unlock?id=${ID}`);
  assert.equal(view.unlock.price_usdc, null);
  assert.equal(view.unlock.amount_atomic, '1450000');
  assert.equal(view.unlock.amount_decimal, '1.45');
  assert.equal(view.ready_at, '2026-10-01T13:12:40+00:00');
  for (const [key, value] of Object.entries(view.funding)) {
    if (key.endsWith('_usdc')) assert.equal(value, null, key);
  }
  assert.equal(view.funding.total_charged_atomic, '100000');
});

test('a ready USDC chain keeps the v2 usdc fields, at the converted price', () => {
  const view = publicView(chain(USDC, { window: 'sla' }));
  assert.equal(view.unlock.price_usdc, '3.40');
  assert.equal(view.funding.entry_charged_usdc, '0.12');
  assert.equal(view.funding.unlock_list_price_usdc, '3.40');
  assert.equal(view.funding.total_list_price_usdc, '3.52');
  assert.equal(view.funding.total_charged_usdc, '0.12');
});

test('a processing chain shows both deadlines and no unlock', () => {
  const view = publicView(chain(EURC, { status: 'pending' }));
  assert.equal(view.status, 'processing');
  assert.equal(view.sla_deadline, '2026-10-01T10:00:02+00:00');
  assert.equal(view.grace_deadline, '2026-10-02T09:00:02+00:00');
  assert.equal(view.expires_at, view.grace_deadline);
  assert.equal(view.unlock, undefined);
  assert.equal(view.service_window, undefined);
  // Before the answer the unlock amount shown is the most it can be.
  assert.equal(view.funding.unlock_amount_atomic, '2900000');
});

test('an expired v3 chain reports the admission credit in its own terms', () => {
  const view = publicView(chain(EURC, { status: 'expired', credit: true }));
  assert.equal(view.status, 'failed');
  assert.equal(view.failure.reason, 'human_timeout');
  assert.equal(view.failure.entry_credit, 'next_admission');
  assert.equal(view.failure.entry_credit_value_usdc, null);
  const usdc = publicView(chain(USDC, { status: 'expired', credit: true }));
  assert.equal(usdc.failure.entry_credit_value_usdc, '0.12');
});

function unlock(base, headers = {}) {
  return fetch(`${base}/verify-unlock?id=${ID}`, { method: 'POST', headers });
}

test('the unlock 402 asks for exactly the bound asset and the decided amount', () => withStack(async ({ core, facilitator, base }) => {
  core.verifies.set(ID, chain(EURC, { window: 'grace' }));
  const res = await unlock(base);
  assert.equal(res.status, 402);
  const required = decodePaymentRequiredHeader(res.headers.get('payment-required'));
  assert.equal(required.accepts.length, 1);
  assert.equal(required.accepts[0].asset, EURC);
  assert.equal(required.accepts[0].amount, '1450000');
  assert.deepEqual(required.accepts[0].extra, { name: 'EURC', version: '2', terms_id: 'st_one' });
  assert.deepEqual(required.extensions['service-windows'].info, {
    version: 1, terms_id: 'st_one', applied: 'grace',
    admitted_at: '2026-10-01T09:00:02+00:00', ready_at: '2026-10-01T13:12:40+00:00',
    network: NETWORK, asset: EURC, unlock_amount: '1450000',
  });
  assert.deepEqual(facilitator.calls, []);
}));

test('paying the unlock releases the result and records the unlock', () => withStack(async ({ core, facilitator, base }) => {
  core.verifies.set(ID, chain(EURC, { window: 'grace' }));
  const first = await unlock(base);
  const payload = await signed(decodePaymentRequiredHeader(first.headers.get('payment-required')));
  const res = await unlock(base, { 'PAYMENT-SIGNATURE': encodePaymentSignatureHeader(payload) });
  assert.equal(res.status, 200);
  const view = await res.json();
  assert.equal(view.status, 'completed');
  assert.equal(view.verdict, 'accepted');
  assert.equal(view.funding.unlock_charged_atomic, '1450000');
  assert.equal(view.funding.total_charged_atomic, '1550000');
  assert.deepEqual(facilitator.calls, [['verify', EURC, '1450000'], ['settle', EURC, '1450000']]);
  await settleRecorded(core);
  assert.deepEqual(core.payments.map((p) => p.kind), ['unlock']);
}));

test('an unlock paid in the other asset matches nothing and is never settled', () => withStack(async ({ core, facilitator, base }) => {
  core.verifies.set(ID, chain(EURC, { window: 'sla' }));
  const first = await unlock(base);
  const payload = await signed(decodePaymentRequiredHeader(first.headers.get('payment-required')));
  const tampered = structuredClone(payload);
  tampered.accepted.asset = USDC;
  tampered.accepted.extra = { ...tampered.accepted.extra, name: 'USD Coin' };
  const res = await unlock(base, { 'PAYMENT-SIGNATURE': encodePaymentSignatureHeader(tampered) });
  assert.equal(res.status, 402);
  assert.deepEqual(facilitator.calls, []);
}));

test('a result that is not ready asks for no payment', () => withStack(async ({ core, facilitator, base }) => {
  core.verifies.set(ID, chain(EURC, { status: 'pending' }));
  const res = await unlock(base);
  assert.equal(res.status, 409);
  assert.equal(res.headers.get('payment-required'), null);
  assert.deepEqual(facilitator.calls, []);
}));

test('a v2 chain still unlocks at its fixed USDC price', () => withStack(async ({ core, base }) => {
  core.verifies.set(ID, {
    ...chain(USDC), contract_version: 2, terms_id: null, bound_terms: null, bound_asset: null,
    applied_window: null, unlock_amount_atomic: null,
  });
  const res = await unlock(base);
  assert.equal(res.status, 402);
  const required = decodePaymentRequiredHeader(res.headers.get('payment-required'));
  assert.deepEqual(required.accepts.map((a) => [a.asset, a.amount]), [[USDC, '2900000']]);
}));
