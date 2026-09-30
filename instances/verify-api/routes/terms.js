/**
 * Contract v3 terms on the x402 gates.
 *
 * The core owns pricing: it builds each quote with the one pricing function
 * and stores it. This module only turns a stored quote into x402 payment
 * requirements and into the `service-windows` extension of the same 402, so
 * the requirements and the advertised terms always describe the same quote.
 *
 * A quote is resolved at most once per request. With no payment header the
 * current quote is advertised. With one, the quote the payer signed against
 * (`accepted.extra.terms_id`) is rebuilt while it is still valid, so the
 * signed requirement matches exactly. An expired or unknown quote falls back
 * to the current one, which the old signature cannot match: x402 then answers
 * a fresh 402 and the facilitator is never called.
 */
import { decodePaymentSignatureHeader } from '@x402/core/http';
import { coreFetch, INSTANCE } from './core.js';

export const SERVICE_WINDOWS = 'service-windows';

export class PricingUnavailableError extends Error {
  constructor(message) {
    super(message);
    this.name = 'PricingUnavailableError';
  }
}

/** The assets offered at gate 1, in `accepts` order. USDC first (Q11). */
export function assetSymbols(env = process.env) {
  return String(env.X402_ASSETS ?? 'USDC,EURC')
    .split(',')
    .map((s) => s.trim().toUpperCase())
    .filter(Boolean);
}

/** JSON Schema for `service-windows.info`: the spec's Appendix A. */
export const INFO_SCHEMA = {
  type: 'object',
  required: ['version', 'terms_id', 'windows', 'prices', 'asset_binding', 'expiry', 'cancellation'],
  properties: {
    version: { const: 1 },
    terms_id: { type: 'string' },
    terms_valid_until: { type: 'string', format: 'date-time' },
    windows: {
      type: 'object',
      required: ['sla'],
      properties: {
        sla: {
          type: 'object',
          required: ['within_seconds'],
          properties: { within_seconds: { type: 'integer', minimum: 1 } },
        },
        grace: {
          type: 'object',
          required: ['within_seconds'],
          properties: { within_seconds: { type: 'integer', minimum: 1 } },
        },
      },
    },
    price_basis: {
      type: 'object',
      required: ['currency', 'admission', 'sla'],
      properties: {
        currency: { type: 'string' },
        admission: { type: 'string' },
        sla: { type: 'string' },
        grace: { type: 'string' },
      },
    },
    prices: {
      type: 'array',
      minItems: 1,
      items: {
        type: 'object',
        required: ['network', 'asset', 'admission', 'sla'],
        properties: {
          network: { type: 'string' },
          asset: { type: 'string' },
          admission: { type: 'string', pattern: '^[0-9]+$' },
          sla: { type: 'string', pattern: '^[0-9]+$' },
          grace: { type: 'string', pattern: '^[0-9]+$' },
          conversion: {
            type: 'object',
            required: ['from', 'rate', 'source', 'as_of'],
            properties: {
              from: { type: 'string' },
              rate: { type: 'string' },
              source: { type: 'string' },
              as_of: { type: 'string' },
              rounding: { enum: ['up_to_cent', 'none'] },
            },
          },
        },
      },
    },
    asset_binding: { enum: ['admission_asset'] },
    expiry: {
      type: 'object',
      properties: { admission_credit: { enum: ['next_admission', 'none'] } },
    },
    cancellation: { enum: ['none', 'after_sla_admission_credit'] },
    status_url_template: { type: 'string' },
    unlock_url_template: { type: 'string' },
  },
};

function decodeAccepted(header) {
  if (!header) return null;
  try {
    return decodePaymentSignatureHeader(String(header))?.accepted ?? null;
  } catch {
    return null;
  }
}

/**
 * What the caller paid with, read after the gate. The middleware has matched
 * `accepted` against a requirement this server built, so its asset and
 * terms_id are the server's own values, not the caller's claims.
 */
export function paidSelection(req) {
  const accepted = decodeAccepted(req.get('payment-signature'));
  if (!accepted) return null;
  return {
    termsId: accepted.extra?.terms_id ?? null,
    asset: accepted.asset ?? null,
    network: accepted.network ?? null,
  };
}

async function fetchQuote(termsId) {
  const path = termsId
    ? `/internal/terms/${encodeURIComponent(termsId)}`
    : `/internal/terms/current?instance=${encodeURIComponent(INSTANCE)}`;
  let result;
  try {
    result = await coreFetch(path);
  } catch (err) {
    throw new PricingUnavailableError(`core did not answer for terms: ${err.message}`);
  }
  if (result.status === 200) return result.body;
  if (termsId && result.status === 404) return null;
  throw new PricingUnavailableError(result.body?.detail ?? `core terms returned ${result.status}`);
}

async function resolveAdmissionQuote(ctx) {
  const accepted = decodeAccepted(ctx.paymentHeader ?? ctx.adapter.getHeader('payment-signature'));
  const paidTermsId = accepted?.extra?.terms_id;
  if (typeof paidTermsId === 'string' && paidTermsId) {
    const paid = await fetchQuote(paidTermsId);
    if (paid?.valid) return paid;
  }
  return fetchQuote(null);
}

// Per-request memo, keyed by the adapter the x402 middleware creates once for
// each request. Both live exactly as long as the request: nothing here is
// state that would need to survive a restart.
const quotes = new WeakMap();
const advertised = new WeakMap();

export function admissionQuote(ctx) {
  let pending = quotes.get(ctx.adapter);
  if (!pending) {
    pending = resolveAdmissionQuote(ctx);
    quotes.set(ctx.adapter, pending);
  }
  return pending;
}

/** One quote price as an x402 AssetAmount, with the EIP-712 domain and terms_id. */
export function assetAmount(quote, symbol, network, gate) {
  const meta = Object.values(quote.internal?.assets ?? {}).find((m) => m.symbol === symbol);
  const price = meta && quote.terms.prices.find(
    (p) => p.asset.toLowerCase() === meta.address.toLowerCase(),
  );
  if (!price || price.network !== network || !price[gate]) {
    throw new PricingUnavailableError(
      `quote ${quote.terms_id} has no ${gate} price for ${symbol} on ${network}; ` +
      'X402_ASSETS or X402_NETWORK differs between core-api and verify-api',
    );
  }
  return {
    asset: price.asset,
    amount: price[gate],
    extra: { name: meta.name, version: meta.version, terms_id: quote.terms_id },
  };
}

/** DynamicPrice for one gate 1 option. */
export function admissionPrice(symbol, network) {
  return async (ctx) => {
    const quote = await admissionQuote(ctx);
    advertised.set(ctx.adapter, quote.terms);
    return assetAmount(quote, symbol, network, 'admission');
  };
}

/** The route declaration. The registered extension fills in the real info. */
export function serviceWindowsDeclaration() {
  return { [SERVICE_WINDOWS]: { info: { version: 1 }, schema: INFO_SCHEMA } };
}

/**
 * Fills `extensions["service-windows"]` from the quote the requirements of
 * this same response were built from. The info is a pure function of the
 * stored quote, so a client that echoes it back passes echo validation.
 */
export const serviceWindowsExtension = {
  key: SERVICE_WINDOWS,
  enrichPaymentRequiredResponse: async (_declaration, context) => {
    const adapter = context.transportContext?.request?.adapter;
    const info = adapter ? advertised.get(adapter) : undefined;
    if (!info) return undefined;
    return { info, schema: INFO_SCHEMA };
  },
};
