/**
 * Verify API: three gates for one verification chain (contract v3).
 *
 * POST /verify admits one request to the human queue. Its price is a quote
 * built by the core in euros and offered in USDC and EURC. POST /verify-unlock
 * unlocks the ready result in the asset the admission was paid in, at the SLA
 * or grace price decided when the human answered. There is no free human work;
 * legacy full-free rows can still finish their old chains.
 *
 * createApp builds the whole app without listening, so tests can run it
 * against a fake core and a fake facilitator.
 */
import express from 'express';
import { paymentMiddleware, x402ResourceServer, RouteConfigurationError } from '@x402/express';
import { ExactEvmScheme } from '@x402/evm/exact/server';
import { HTTPFacilitatorClient } from '@x402/core/server';
import { declareDiscoveryExtension, bazaarResourceServerExtension } from '@x402/extensions/bazaar';
import {
  verifyRouter,
  quotaFor,
  getVerify,
  handleVerify,
  coreFetch,
  publicView,
  agentStatus,
  recordSettlementOnFinish,
  admissionRoute,
  isWallet,
  WALLET_ADDRESS_ERROR,
  auditRequest,
} from './routes/verify.js';
import {
  admissionPrice,
  assetSymbols,
  currentTermsHandler,
  PricingUnavailableError,
  serviceWindowsDeclaration,
  serviceWindowsExtension,
  UNLOCK_INFO_SCHEMA,
  unlockPrice,
  unlockBandPrice,
} from './routes/terms.js';
import { proveWallet } from './routes/proof.js';

const UUID_RE = /^[0-9a-f-]{36}$/i;

/** Bazaar discovery: how to call gate 1, so crawlers can list it correctly. */
function discoveryDeclaration() {
  return declareDiscoveryExtension({
    bodyType: 'json',
    input: {
      intent: 'Check that this claim is true before I act on it.',
      claim: 'The Eiffel Tower is in Paris.',
      agent_id: '0x0000000000000000000000000000000000000001',
    },
    inputSchema: {
      type: 'object',
      required: ['intent', 'claim', 'agent_id'],
      additionalProperties: false,
      properties: {
        intent: { type: 'string', minLength: 1, maxLength: 2000, description: 'What the agent wants to do and why it needs a human check.' },
        claim: { type: 'string', minLength: 1, maxLength: 4000, description: 'The statement a human should verify.' },
        agent_id: { type: 'string', pattern: '^0x[0-9a-fA-F]{40}$', description: 'The requester wallet address.' },
        callback_url: { type: 'string', pattern: '^https://', maxLength: 2048, description: 'Optional https URL notified when the result is ready or failed. Polling always works.' },
      },
    },
    output: {
      example: {
        verify_id: '0f3f7d0a-0000-4000-8000-000000000000',
        status: 'processing',
        next_action: 'poll',
        poll_url: '/verify/0f3f7d0a-0000-4000-8000-000000000000',
        contract_version: 3,
      },
    },
  });
}

/** Bazaar discovery for the unlock: no body, the chain is named by ?id=. */
function unlockDiscoveryDeclaration() {
  return declareDiscoveryExtension({
    bodyType: 'json',
    input: {},
    inputSchema: {
      type: 'object',
      additionalProperties: false,
      properties: {},
      description: 'No body. Name the ready chain with the id query parameter: POST /verify-unlock?id=<verify_id>.',
    },
    output: {
      example: {
        verify_id: '0f3f7d0a-0000-4000-8000-000000000000',
        status: 'completed',
        verdict: 'refined',
        explanation: 'Use the corrected delivery date: 22 July.',
        next_action: 'done',
        contract_version: 3,
      },
    },
  });
}

export function createApp({ env = process.env, facilitatorClient } = {}) {
  const X402_PAY_TO = env.X402_PAY_TO ?? '';
  const X402_UNLOCK_PRICE = env.X402_UNLOCK_PRICE ?? '$2.90';
  const X402_NETWORK = env.X402_NETWORK ?? 'eip155:8453';
  const FACILITATOR_URL = env.FACILITATOR_URL ?? 'https://x402.org/facilitator';

  const app = express();
  app.set('trust proxy', 1);
  app.use(express.json({ limit: '32kb' }));

  app.get('/health', (_req, res) => res.json({ ok: true }));
  app.get('/terms', currentTermsHandler);

  // Audit every admission and unlock request, including the 402 challenges that
  // let an agent test the payment path. Registered first on each route so the
  // finish hook is attached before any handler can respond.
  app.post('/verify', auditRequest('/verify'));
  app.post('/verify-unlock', auditRequest('/verify-unlock'));

  // Admission preflight decides which gate a request goes through. It answers
  // only what it can answer from the wallet's own quota, and everything else
  // falls through to x402 so that an unpaid request always ends at a real 402
  // with the payment requirements. Nothing here can charge the caller: x402
  // cancels settlement for any 4xx or 5xx produced after the gate, so the checks
  // that run later are just as free as the ones that run before it.
  app.post('/verify', async (req, res, next) => {
    try {
      const agentId = req.body?.agent_id;
      // A request that does not name a wallet cannot be measured against a
      // quota, so it goes straight to the payment gate. handleVerify rejects the
      // malformed agent_id after payment is verified but before it settles.
      if (!isWallet(agentId)) return next('route');

      const quota = await quotaFor(agentId);
      // Prefer the free/credit path only when an entitlement can actually be
      // consumed. When the platform's daily free budget is spent, a wallet with
      // free allowance left falls through to x402 so it can still pay to proceed.
      switch (admissionRoute(quota)) {
        case 'active-chain':
          return res.status(429).json({
            error: 'one active verify per agent_id',
            detail: 'Poll the previous verify until it is completed or failed.',
          });
        case 'entitlement': {
          // A free admission is spent only by the wallet's owner. The wallet
          // gets the same 402 as everyone, signs it, and the signature is
          // checked here instead of being settled, so no funds move. Without
          // paid gates there is no 402 to sign, and the address is trusted.
          if (X402_PAY_TO) {
            const proof = await proveWallet(req, { agentId, payTo: X402_PAY_TO, network: X402_NETWORK });
            if (!proof.ok) return next('route');
            req.walletProven = true;
          }
          req.admissionMode = 'entitlement';
          return await handleVerify(req, res);
        }
        default:
          return next('route');
      }
    } catch (err) {
      console.error('admission preflight failed:', err.message);
      return res.status(502).json({ error: 'verification backend unavailable' });
    }
  });

  // The full-free entitlement unlock works even when paid x402 routes are not
  // configured. Other ready results fall through to the paid route.
  app.post('/verify-unlock', async (req, res, next) => {
    const id = String(req.query.id ?? '');
    // With paid gates on, a request that names no chain goes to the unlock
    // band gate instead of a 400 or 404: a cold probe then sees a real 402
    // with both unlock prices. Paying it unlocks nothing; the handler refuses
    // and x402 cancels settlement for that 4xx.
    if (!UUID_RE.test(id)) {
      if (X402_PAY_TO) {
        req.unlockProbe = 'bad_id';
        return next('route');
      }
      return res.status(400).json({ error: 'pass the verify id as ?id=<uuid>' });
    }
    if (req.auditContext) req.auditContext.verify_id = id;
    const { status, body } = await getVerify(id);
    if (status === 404) {
      if (X402_PAY_TO) {
        req.unlockProbe = 'not_found';
        return next('route');
      }
      return res.status(404).json({ error: 'verify not found' });
    }
    if (status !== 200) return res.status(502).json({ error: 'verification backend unavailable' });
    if (req.auditContext) {
      req.auditContext.verify_no = body.verify_no ?? null;
      req.auditContext.agent_id = body.agent_id ?? null;
      req.auditContext.admission_source = body.entry_source ?? null;
      // Looking up a paid verify proves nothing about the caller of this unlock
      // attempt. Ownership becomes true only after this request passes x402.
      req.auditContext.wallet_ownership_proven = false;
    }
    const publicStatus = agentStatus(body);
    if (publicStatus === 'completed') return res.json(publicView(body));
    if (publicStatus !== 'ready') {
      return res.status(409).json({
        error: publicStatus === 'failed' ? 'failed verifies cannot be unlocked' : 'result is not ready',
        status: publicStatus,
      });
    }
    req.unlockVerifyId = id;
    req.unlockVerify = body;
    if (body.entry_source !== 'initial_free') return next('route');

    const unlocked = await coreFetch(`/internal/verifies/${id}/entitlement-unlock`, {
      method: 'POST',
      body: JSON.stringify({ source: 'initial_free' }),
    });
    if (unlocked.status !== 200) {
      return res.status(unlocked.status === 409 ? 409 : 502).json({
        error: unlocked.body.detail ?? 'free unlock failed',
      });
    }
    return res.json(publicView(unlocked.body));
  });

  let checkFacilitatorSupport = async () => {};

  if (X402_PAY_TO) {
    const facilitator = facilitatorClient ?? new HTTPFacilitatorClient({ url: FACILITATOR_URL });
    const resourceServer = new x402ResourceServer(facilitator)
      .register(X402_NETWORK, new ExactEvmScheme())
      .registerExtension(serviceWindowsExtension)
      .registerExtension(bazaarResourceServerExtension);

    // The facilitator decides which scheme and network it actually serves. x402
    // validates the routes against that list on the first paid request and
    // throws, so a mainnet price behind a testnet-only facilitator breaks both
    // gates at runtime with nothing in the logs to explain it. Ask at startup
    // instead. Nothing is cached: this only reports, the middleware still
    // decides per request.
    checkFacilitatorSupport = async () => {
      try {
        const { kinds = [] } = (await facilitator.getSupported()) ?? {};
        if (kinds.some((k) => k.scheme === 'exact' && k.network === X402_NETWORK)) {
          console.log(`facilitator serves exact on ${X402_NETWORK}`);
          return;
        }
        const offered = kinds
          .filter((k) => k.scheme === 'exact')
          .map((k) => k.network)
          .join(', ');
        console.error(
          `FACILITATOR MISMATCH: ${FACILITATOR_URL} does not serve scheme exact on ` +
          `${X402_NETWORK}, so both paid gates refuse every request until ` +
          `FACILITATOR_URL or X402_NETWORK changes. Free chains are unaffected. ` +
          `Networks offered for exact: ${offered || 'none'}.`,
        );
      } catch (err) {
        console.error(`facilitator ${FACILITATOR_URL} did not answer /supported: ${err.message}`);
      }
    };

    // Gate 1. One option per accepted asset, in X402_ASSETS order, each priced
    // from the same stored quote. USDC comes first because x402 clients that
    // do not choose pay with the first option (Q11).
    app.post(
      '/verify',
      paymentMiddleware(
        {
          'POST /verify': {
            accepts: assetSymbols(env).map((symbol) => ({
              scheme: 'exact',
              price: admissionPrice(symbol, X402_NETWORK),
              network: X402_NETWORK,
              payTo: X402_PAY_TO,
            })),
            description: 'Gate 1 of one Verifi chain. Admit one request to the human queue.',
            mimeType: 'application/json',
            extensions: { ...serviceWindowsDeclaration(), ...discoveryDeclaration() },
          },
        },
        resourceServer,
      ),
      (req, res) => {
        req.admissionMode = 'x402';
        return handleVerify(req, res);
      },
    );

    // Gates 2 and 3, for a named ready chain: exactly one option, the bound
    // asset at the amount decided when the human answered.
    const unlockRecordGate = paymentMiddleware(
      {
        'POST /verify-unlock': {
          accepts: {
            scheme: 'exact',
            price: unlockPrice(X402_UNLOCK_PRICE, X402_NETWORK),
            network: X402_NETWORK,
            payTo: X402_PAY_TO,
          },
          description:
            'Gates 2 and 3 of the same Verifi chain. Unlock its ready human result at the SLA ' +
            'or grace price decided when the human answered, in the asset the admission was paid in.',
          mimeType: 'application/json',
          extensions: serviceWindowsDeclaration(UNLOCK_INFO_SCHEMA),
        },
      },
      resourceServer,
    );

    // The unlock band, for a request that names no chain (a cold probe):
    // every asset at both window prices, so a directory sees a real 402. The
    // handler below refuses any payment made here, so it can never settle.
    const unlockBandGate = paymentMiddleware(
      {
        'POST /verify-unlock': {
          accepts: assetSymbols(env).flatMap((symbol) => ['sla', 'grace'].map((window) => ({
            scheme: 'exact',
            price: unlockBandPrice(symbol, X402_NETWORK, window),
            network: X402_NETWORK,
            payTo: X402_PAY_TO,
          }))),
          description:
            'Unlock a ready Verifi result: 2.90 EUR if the human answered within 60 minutes of ' +
            'admission, 1.45 EUR within 24 hours. Name the chain with ?id=<verify_id>; the 402 for ' +
            'a named ready chain asks for its one exact price.',
          mimeType: 'application/json',
          extensions: { ...serviceWindowsDeclaration(), ...unlockDiscoveryDeclaration() },
          unpaidResponseBody: async () => ({
            contentType: 'application/json',
            body: {
              error: 'pass the verify id of a ready chain as ?id=<uuid>',
              detail: 'This 402 shows the unlock price band. A payment without a ready chain is refused and never taken.',
            },
          }),
        },
      },
      resourceServer,
    );

    app.post(
      '/verify-unlock',
      (req, res, next) => (req.unlockProbe ? unlockBandGate : unlockRecordGate)(req, res, next),
      (req, res) => {
        if (req.unlockProbe) {
          // A payment against the band names no ready chain. Refusing with a
          // 4xx makes x402 cancel settlement, so the signed authorization is
          // never submitted and no funds move.
          return req.unlockProbe === 'not_found'
            ? res.status(404).json({ error: 'verify not found', detail: 'No payment was taken.' })
            : res.status(400).json({ error: 'pass the verify id as ?id=<uuid>', detail: 'No payment was taken.' });
        }
        // This handler runs only after the current unlock request's x402
        // signature has been verified. A previous paid entry is not proof that
        // an unauthenticated caller controls the wallet.
        if (req.auditContext) req.auditContext.wallet_ownership_proven = true;
        recordSettlementOnFinish(res, 'unlock', () => req.unlockVerifyId);
        // x402 buffers this body and only releases it after settlement succeeds.
        // It is therefore safe to include the result here before the finish
        // hook records the transaction in PostgreSQL.
        return res.json(publicView(req.unlockVerify, { forceUnlocked: true }));
      },
    );

    console.log(
      `x402 three-gate flow active: quoted in ${assetSymbols(env).join(', ')}, ` +
      `legacy v2 unlock ${X402_UNLOCK_PRICE}, ${X402_NETWORK} via ${FACILITATOR_URL}`,
    );
  } else {
    // Without a payment gate there is nothing left to fall through to, so this
    // route answers the rejections handleVerify would otherwise have made.
    app.post('/verify', (req, res) => {
      if (!isWallet(req.body?.agent_id)) {
        return res.status(400).json({ error: WALLET_ADDRESS_ERROR });
      }
      res.status(503).json({ error: 'paid admission is not configured and no entitlement remains' });
    });
    app.post('/verify-unlock', (_req, res) => {
      res.status(503).json({ error: 'paid unlock is not configured' });
    });
    console.warn('X402_PAY_TO not set: paid admission is off and no free tier exists, so /verify only serves earned-credit chains');
  }

  app.post('/verify/:id/unlock', (req, res) => {
    res.redirect(308, `/verify-unlock?id=${encodeURIComponent(req.params.id)}`);
  });

  app.use('/', verifyRouter);

  // Every answer this API gives an agent is JSON. Without this handler express
  // renders its default HTML page with a stack trace, which is what a route
  // rejected by the facilitator produced: a public 500 carrying internal paths
  // instead of a machine-readable error. Nothing here can have charged the
  // caller, because x402 settles only after a route returns below 400.
  app.use((err, _req, res, _next) => {
    const misconfigured = err instanceof RouteConfigurationError;
    const unpriced = err instanceof PricingUnavailableError;
    console.error(
      misconfigured ? 'x402 routes rejected:' : unpriced ? 'pricing unavailable:' : 'unhandled error:',
      err.message,
    );
    if (res.headersSent) return res.end();
    if (misconfigured) {
      res.set('Retry-After', '120');
      return res.status(503).json({
        error: 'paid gates are unavailable',
        detail: 'The configured facilitator does not serve this network. No payment was taken.',
      });
    }
    if (unpriced) {
      // The price function runs before any payment is verified, so this
      // refusal never follows a charge.
      res.set('Retry-After', '60');
      return res.status(503).json({
        error: 'pricing is temporarily unavailable',
        detail: 'No quote can be issued right now. Retry shortly. No payment was taken.',
      });
    }
    return res.status(500).json({ error: 'internal error' });
  });

  return { app, checkFacilitatorSupport };
}
