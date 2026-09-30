/**
 * Shared scaffolding for the in-process x402 tests: a fake core, a fake
 * facilitator, the real verify-api app, and a real x402 client with a key
 * generated per run. Nothing leaves this machine.
 */
import assert from 'node:assert/strict';
import express from 'express';
import { x402Client } from '@x402/core/client';
import { ExactEvmScheme } from '@x402/evm/exact/client';
import { decodePaymentRequiredHeader } from '@x402/core/http';
import { generatePrivateKey, privateKeyToAccount } from 'viem/accounts';

import { createApp } from '../app.js';

export const USDC = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913';
export const EURC = '0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42';
export const PAY_TO = '0x000000000000000000000000000000000000dEaD';
export const NETWORK = 'eip155:8453';

export function quote(termsId) {
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

export function boundTerms(q, asset) {
  const price = q.terms.prices.find((p) => p.asset === asset);
  return {
    version: 1, terms_id: q.terms_id, windows: q.terms.windows, price_basis: q.terms.price_basis,
    network: NETWORK, asset, asset_symbol: asset === USDC ? 'USDC' : 'EURC', asset_decimals: 6,
    asset_eip712: asset === USDC ? { name: 'USD Coin', version: '2' } : { name: 'EURC', version: '2' },
    admission: price.admission, sla: price.sla, grace: price.grace,
    asset_binding: 'admission_asset', expiry: q.terms.expiry, cancellation: 'none',
  };
}

/** A core that answers the calls verify-api makes, and remembers them. */
export async function startFakeCore() {
  const state = { quotes: new Map(), current: null, termsStatus: 200, created: [], payments: [], verifies: new Map() };
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
  core.get('/internal/verifies/:id', (req, res) => {
    const v = state.verifies.get(req.params.id);
    return v ? res.json(v) : res.status(404).json({ detail: 'verify not found' });
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

export function fakeFacilitator() {
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

export async function startVerifyApi(core, facilitator, extraEnv = {}) {
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

export const account = privateKeyToAccount(generatePrivateKey());
export const body = JSON.stringify({ intent: 'Check before acting.', claim: 'The sky is green.', agent_id: account.address });

export function post(base, headers = {}) {
  return fetch(`${base}/verify`, { method: 'POST', headers: { 'content-type': 'application/json', ...headers }, body });
}

export async function challenge(base) {
  const res = await post(base);
  assert.equal(res.status, 402);
  return decodePaymentRequiredHeader(res.headers.get('payment-required'));
}

export async function signed(paymentRequired, selector) {
  const client = new x402Client(selector).register('eip155:*', new ExactEvmScheme(account));
  return client.createPaymentPayload(paymentRequired);
}

// Settlement is recorded from a finish hook, after the response is sent.
export async function settleRecorded(core) {
  for (let i = 0; i < 50 && core.payments.length === 0; i += 1) {
    await new Promise((r) => setTimeout(r, 20));
  }
}

export async function withStack(fn, extraEnv) {
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

