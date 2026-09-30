/**
 * Gate 1 end to end, in process: the real x402 middleware and a real x402
 * client, against a fake core and a fake facilitator. Nothing leaves this
 * machine and no key is real: the payer is generated per run.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import express from 'express';
import { x402Client } from '@x402/core/client';
import { ExactEvmScheme } from '@x402/evm/exact/client';
import { decodePaymentRequiredHeader, encodePaymentSignatureHeader } from '@x402/core/http';
import { generatePrivateKey, privateKeyToAccount } from 'viem/accounts';

import { createApp } from '../app.js';
import { INFO_SCHEMA } from './terms.js';

const USDC = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913';
const EURC = '0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42';
const PAY_TO = '0x52251E52336EE476dAd57241252Ad353F874E9c1';
const NETWORK = 'eip155:8453';

function quote(termsId) {
  return {
    terms_id: termsId,
    valid: true,
    valid_until: '2026-10-01T09:10:00+00:00',
    terms: {
      version: 1,
      terms_id: termsId,
      terms_valid_until: '2026-10-01T09:10:00.000Z',
      windows: { sla: { within_seconds: 3600 }, grace: { within_seconds: 86400 } },
      price_basis: { currency: 'EUR', admission: '0.10', sla: '2.90', grace: '1.45' },
      prices: [
        {
          network: NETWORK, asset: USDC, admission: '120000', sla: '3400000', grace: '1700000',
          conversion: { from: 'EUR', rate: '1.17', source: 'ECB euro reference rate', as_of: '2026-09-30', rounding: 'up_to_cent' },
        },
        { network: NETWORK, asset: EURC, admission: '100000', sla: '2900000', grace: '1450000' },
      ],
      asset_binding: 'admission_asset',
      expiry: { admission_credit: 'next_admission' },
      cancellation: 'none',
      status_url_template: 'https://verifi.cloud/verify/{work_id}',
      unlock_url_template: 'https://verifi.cloud/verify-unlock?id={work_id}',
    },
    internal: {
      assets: {
        [USDC.toLowerCase()]: { symbol: 'USDC', address: USDC, decimals: 6, name: 'USD Coin', version: '2', currency: 'USD' },
        [EURC.toLowerCase()]: { symbol: 'EURC', address: EURC, decimals: 6, name: 'EURC', version: '2', currency: 'EUR' },
      },
      responder_commission_eur: '0.50',
    },
  };
}

function boundTerms(q, asset) {
  const price = q.terms.prices.find((p) => p.asset === asset);
  return {
    version: 1, terms_id: q.terms_id, windows: q.terms.windows, price_basis: q.terms.price_basis,
    network: NETWORK, asset, asset_symbol: asset === USDC ? 'USDC' : 'EURC', asset_decimals: 6,
    admission: price.admission, sla: price.sla, grace: price.grace,
    asset_binding: 'admission_asset', expiry: q.terms.expiry, cancellation: 'none',
  };
}

/** A core that answers the calls verify-api makes, and remembers them. */
async function startFakeCore() {
  const state = { quotes: new Map(), current: null, termsStatus: 200, created: [], payments: [] };
  const core = express();
  core.use(express.json());
  core.get('/internal/terms/current', (_req, res) => {
    if (state.termsStatus !== 200) return res.status(state.termsStatus).json({ detail: 'no rate yet' });
    return res.json(state.current);
  });
  core.get('/internal/terms/:id', (req, res) => {
    const q = state.quotes.get(req.params.id);
    return q ? res.json(q) : res.status(404).json({ detail: 'terms not found' });
  });
  core.get('/internal/quota', (_req, res) => res.json({
    pending_count: 0, has_entry_entitlement: false, entitlement_admission_available: false,
  }));
  core.post('/internal/request-audit', (_req, res) => res.json({ ok: true }));
  core.post('/internal/verifies', (req, res) => {
    state.created.push(req.body);
    const q = state.quotes.get(req.body.terms_id);
    const bound = q && boundTerms(q, req.body.paid_asset);
    return res.json({
      id: '0f3f7d0a-0000-4000-8000-000000000001', verify_no: 1, status: 'admission_pending',
      agent_id: req.body.agent_id, entry_source: 'x402', entry_charged_usdc: '0.00',
      contract_version: 3, terms_id: req.body.terms_id, bound_terms: bound,
      bound_asset: req.body.paid_asset, bound_network: NETWORK,
      entry_amount_atomic: bound?.admission, entry_charged_atomic: null,
      admitted_at: null, sla_deadline: null, grace_deadline: null, expires_at: null,
      created_at: '2026-10-01T09:00:00+00:00', result_unlocked: false,
    });
  });
  core.post('/internal/verifies/:id/payment', (req, res) => {
    state.payments.push({ id: req.params.id, ...req.body });
    return res.json({ ok: true });
  });
  const server = core.listen(0, '127.0.0.1');
  await new Promise((r) => server.once('listening', r));
  state.url = `http://127.0.0.1:${server.address().port}`;
  state.close = () => new Promise((r) => { server.close(r); server.closeAllConnections(); });
  state.publish = (q) => {
    state.quotes.set(q.terms_id, q);
    state.current = q;
  };
  return state;
}

function fakeFacilitator() {
  const calls = [];
  return {
    calls,
    async getSupported() {
      return { kinds: [{ x402Version: 2, scheme: 'exact', network: NETWORK }], extensions: [], signers: {} };
    },
    async verify(payload, req) {
      calls.push(['verify', req.asset, req.amount]);
      return { isValid: true, payer: payload.payload?.authorization?.from };
    },
    async settle(payload, req) {
      calls.push(['settle', req.asset, req.amount]);
      return { success: true, transaction: `0x${'ab'.repeat(32)}`, network: req.network, payer: payload.payload?.authorization?.from };
    },
  };
}

async function startVerifyApi(core, facilitator, extraEnv = {}) {
  process.env.CORE_API_URL = core.url;
  const { app } = createApp({
    env: { X402_PAY_TO: PAY_TO, X402_NETWORK: NETWORK, X402_ASSETS: 'USDC,EURC', ...extraEnv },
    facilitatorClient: facilitator,
  });
  const server = app.listen(0, '127.0.0.1');
  await new Promise((r) => server.once('listening', r));
  const base = `http://127.0.0.1:${server.address().port}`;
  return { base, close: () => new Promise((r) => { server.close(r); server.closeAllConnections(); }) };
}

const account = privateKeyToAccount(generatePrivateKey());
const body = JSON.stringify({ intent: 'Check before acting.', claim: 'The sky is green.', agent_id: account.address });

function post(base, headers = {}) {
  return fetch(`${base}/verify`, { method: 'POST', headers: { 'content-type': 'application/json', ...headers }, body });
}

async function challenge(base) {
  const res = await post(base);
  assert.equal(res.status, 402);
  return decodePaymentRequiredHeader(res.headers.get('payment-required'));
}

async function signed(paymentRequired, selector) {
  const client = new x402Client(selector).register('eip155:*', new ExactEvmScheme(account));
  return client.createPaymentPayload(paymentRequired);
}

// Settlement is recorded from a finish hook, after the response is sent.
async function settleRecorded(core) {
  for (let i = 0; i < 50 && core.payments.length === 0; i += 1) {
    await new Promise((r) => setTimeout(r, 20));
  }
}

async function withStack(fn, extraEnv) {
  const core = await startFakeCore();
  const facilitator = fakeFacilitator();
  const api = await startVerifyApi(core, facilitator, extraEnv);
  try {
    core.publish(quote('st_one'));
    await fn({ core, facilitator, base: api.base });
  } finally {
    await api.close();
    await core.close();
  }
}

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
  assert.equal(core.payments[0].kind, 'entry');
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
