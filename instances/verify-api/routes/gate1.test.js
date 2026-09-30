/**
 * Gate 1 end to end, in process: the real x402 middleware and a real x402
 * client, against a fake core and a fake facilitator. Nothing leaves this
 * machine and no key is real: the payer is generated per run.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { decodePaymentRequiredHeader, encodePaymentSignatureHeader } from '@x402/core/http';

import { INFO_SCHEMA } from './terms.js';
import {
  USDC, EURC, quote, post, challenge, signed, settleRecorded, withStack,
} from './testkit.js';

test('an unpaid probe gets both assets, USDC first, and the full terms', () => withStack(async ({ core, facilitator, base }) => {
  const required = await challenge(base);
  assert.deepEqual(required.accepts.map((a) => [a.asset, a.amount]), [[USDC, '120000'], [EURC, '100000']]);
  assert.deepEqual(required.accepts[0].extra, { name: 'USD Coin', version: '2', terms_id: 'st_one' });
  assert.deepEqual(required.accepts[1].extra, { name: 'EURC', version: '2', terms_id: 'st_one' });
  const windows = required.extensions['service-windows'];
  assert.deepEqual(windows.info, quote('st_one').terms);
  assert.deepEqual(windows.schema, INFO_SCHEMA);
  // For each asset the admission price in the terms equals its accepts amount.
  for (const accept of required.accepts) {
    assert.equal(windows.info.prices.find((p) => p.asset === accept.asset).admission, accept.amount);
  }
  const bazaar = required.extensions.bazaar;
  assert.ok(bazaar, 'bazaar discovery is declared');
  assert.deepEqual(bazaar.info.input.body.agent_id, '0x0000000000000000000000000000000000000001');
  assert.equal(facilitator.calls.length, 0);
  assert.equal(core.created.length, 0);
}));

test('a v2 client that pays the first option is admitted in USDC under the quote', () => withStack(async ({ core, facilitator, base }) => {
  const payload = await signed(await challenge(base));
  assert.equal(payload.accepted.asset, USDC);
  // The real client echoes the extension; the echo must validate.
  assert.ok(payload.extensions?.['service-windows']);
  const res = await post(base, { 'PAYMENT-SIGNATURE': encodePaymentSignatureHeader(payload) });
  assert.equal(res.status, 202);
  const view = await res.json();
  assert.equal(view.contract_version, 3);
  assert.equal(view.terms.asset, USDC);
  assert.equal(view.terms.sla, '3400000');
  assert.equal(view.funding.entry_charged_usdc, '0.12');
  assert.equal(view.response_timeout_ms, 86_400_000);
  assert.deepEqual(core.created.map((c) => [c.terms_id, c.paid_asset, c.admission_mode]), [['st_one', USDC, 'x402']]);
  assert.deepEqual(facilitator.calls, [['verify', USDC, '120000'], ['settle', USDC, '120000']]);
  await settleRecorded(core);
  assert.deepEqual(core.payments.map((p) => p.kind), ['entry']);
}));

test('a client that chooses EURC binds the chain to EURC', () => withStack(async ({ core, facilitator, base }) => {
  const payload = await signed(await challenge(base), (_v, accepts) => accepts.find((a) => a.asset === EURC));
  const res = await post(base, { 'PAYMENT-SIGNATURE': encodePaymentSignatureHeader(payload) });
  assert.equal(res.status, 202);
  const view = await res.json();
  assert.equal(view.terms.asset, EURC);
  // An EURC amount is never reported as USDC.
  assert.equal(view.funding.entry_charged_usdc, null);
  assert.equal(core.created[0].paid_asset, EURC);
  assert.deepEqual(facilitator.calls, [['verify', EURC, '100000'], ['settle', EURC, '100000']]);
  await settleRecorded(core);
}));

test('a payment against an expired quote gets fresh terms and is never settled', () => withStack(async ({ core, facilitator, base }) => {
  const payload = await signed(await challenge(base));
  core.quotes.get('st_one').valid = false;
  core.publish(quote('st_two'));
  const res = await post(base, { 'PAYMENT-SIGNATURE': encodePaymentSignatureHeader(payload) });
  assert.equal(res.status, 402);
  const fresh = decodePaymentRequiredHeader(res.headers.get('payment-required'));
  assert.equal(fresh.accepts[0].extra.terms_id, 'st_two');
  assert.equal(fresh.extensions['service-windows'].info.terms_id, 'st_two');
  assert.deepEqual(facilitator.calls, []);
  assert.equal(core.created.length, 0);
}));

test('a payment still inside its quote is honoured after a newer quote appears', () => withStack(async ({ core, facilitator, base }) => {
  const payload = await signed(await challenge(base));
  core.publish(quote('st_two'));
  const res = await post(base, { 'PAYMENT-SIGNATURE': encodePaymentSignatureHeader(payload) });
  assert.equal(res.status, 202);
  assert.equal(core.created[0].terms_id, 'st_one');
  assert.equal(facilitator.calls.length, 2);
  await settleRecorded(core);
}));

test('a payment that swaps the asset matches nothing and is never settled', () => withStack(async ({ core, facilitator, base }) => {
  const payload = await signed(await challenge(base));
  const tampered = structuredClone(payload);
  tampered.accepted.asset = EURC;
  const res = await post(base, { 'PAYMENT-SIGNATURE': encodePaymentSignatureHeader(tampered) });
  assert.equal(res.status, 402);
  assert.deepEqual(facilitator.calls, []);
  assert.equal(core.created.length, 0);
}));

test('without a quote the gate answers 503, never a partial 402', () => withStack(async ({ core, facilitator, base }) => {
  core.termsStatus = 503;
  const res = await post(base);
  assert.equal(res.status, 503);
  assert.equal(res.headers.get('retry-after'), '60');
  assert.equal(res.headers.get('payment-required'), null);
  assert.match((await res.json()).detail, /No payment was taken/);
  assert.deepEqual(facilitator.calls, []);
}));

test('an asset the core did not price is a configuration error, not a 402', () => withStack(async ({ base }) => {
  const res = await post(base);
  assert.equal(res.status, 503);
}, { X402_ASSETS: 'USDC,EURC,DAI' }));

test('X402_ASSETS decides the order of the offer', () => withStack(async ({ base }) => {
  const required = await challenge(base);
  assert.deepEqual(required.accepts.map((a) => a.asset), [EURC, USDC]);
}, { X402_ASSETS: 'EURC,USDC' }));

test('GET /terms returns the current public terms without internals', () => withStack(async ({ core, base }) => {
  const res = await fetch(`${base}/terms`);
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.deepEqual(body, { contract_version: 3, terms: quote('st_one').terms });
  core.termsStatus = 503;
  assert.equal((await fetch(`${base}/terms`)).status, 503);
}));
