/**
 * Verify API with two explicit gates for one verification chain.
 *
 * POST /verify costs 0.10 USDC and only then enters the human queue.
 * POST /verify-unlock costs 2.90 USDC after the result is ready. The first
 * five chains per wallet use a full-free entitlement at both gates.
 */
import express from 'express';
import { paymentMiddleware, x402ResourceServer, RouteConfigurationError } from '@x402/express';
import { ExactEvmScheme } from '@x402/evm/exact/server';
import { HTTPFacilitatorClient } from '@x402/core/server';
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
} from './routes/verify.js';

const PORT = Number(process.env.PORT ?? 8702);
const HOST = process.env.HOST ?? '127.0.0.1';
const X402_PAY_TO = process.env.X402_PAY_TO ?? '';
const X402_ENTRY_PRICE = process.env.X402_ENTRY_PRICE ?? process.env.X402_PRICE ?? '$0.10';
const X402_UNLOCK_PRICE = process.env.X402_UNLOCK_PRICE ?? '$2.90';
const X402_NETWORK = process.env.X402_NETWORK ?? 'eip155:8453';
const FACILITATOR_URL = process.env.FACILITATOR_URL ?? 'https://x402.org/facilitator';

const UUID_RE = /^[0-9a-f-]{36}$/i;

// x402 validates its routes against the facilitator in a promise created when
// the middleware is built, not when a request arrives. A facilitator that
// rejects the configured network, or that is simply unreachable at boot,
// therefore rejects a promise nothing is awaiting yet, and node kills the
// process for it. Free chains do not depend on the facilitator at all, so log
// loudly and keep serving instead of crash looping.
process.on('unhandledRejection', (reason) => {
  console.error('unhandled rejection, still serving:', reason?.message ?? reason);
});

const app = express();
app.set('trust proxy', 1);
app.use(express.json({ limit: '32kb' }));

app.get('/health', (_req, res) => res.json({ ok: true }));

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
      case 'entitlement':
        req.admissionMode = 'entitlement';
        return await handleVerify(req, res);
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
  if (!UUID_RE.test(id)) {
    return res.status(400).json({ error: 'pass the verify id as ?id=<uuid>' });
  }
  const { status, body } = await getVerify(id);
  if (status === 404) return res.status(404).json({ error: 'verify not found' });
  if (status !== 200) return res.status(502).json({ error: 'verification backend unavailable' });
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
  const facilitatorClient = new HTTPFacilitatorClient({ url: FACILITATOR_URL });
  const resourceServer = new x402ResourceServer(facilitatorClient)
    .register(X402_NETWORK, new ExactEvmScheme());

  // The facilitator decides which scheme and network it actually serves. x402
  // validates the routes against that list on the first paid request and
  // throws, so a mainnet price behind a testnet-only facilitator breaks both
  // gates at runtime with nothing in the logs to explain it. Ask at startup
  // instead. Nothing is cached: this only reports, the middleware still
  // decides per request.
  checkFacilitatorSupport = async () => {
    try {
      const { kinds = [] } = (await facilitatorClient.getSupported()) ?? {};
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

  app.post(
    '/verify',
    paymentMiddleware(
      {
        'POST /verify': {
          accepts: {
            scheme: 'exact',
            price: X402_ENTRY_PRICE,
            network: X402_NETWORK,
            payTo: X402_PAY_TO,
          },
          description: 'Gate 1 of one Verifi chain. Admit one request to the human queue.',
        },
      },
      resourceServer,
    ),
    (req, res) => {
      req.admissionMode = 'x402';
      return handleVerify(req, res);
    },
  );

  app.post(
    '/verify-unlock',
    paymentMiddleware(
      {
        'POST /verify-unlock': {
          accepts: {
            scheme: 'exact',
            price: X402_UNLOCK_PRICE,
            network: X402_NETWORK,
            payTo: X402_PAY_TO,
          },
          description: 'Gate 2 of the same Verifi chain. Unlock its ready human result.',
        },
      },
      resourceServer,
    ),
    (req, res) => {
      recordSettlementOnFinish(res, 'unlock', () => req.unlockVerifyId);
      // x402 buffers this body and only releases it after settlement succeeds.
      // It is therefore safe to include the result here before the finish
      // hook records the transaction in PostgreSQL.
      return res.json(publicView(req.unlockVerify, { forceUnlocked: true }));
    },
  );

  console.log(
    `x402 two-gate flow active: entry ${X402_ENTRY_PRICE}, unlock ${X402_UNLOCK_PRICE}, ` +
    `${X402_NETWORK} via ${FACILITATOR_URL}`,
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
  console.warn('X402_PAY_TO not set: only full-free chains are available');
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
  console.error(misconfigured ? 'x402 routes rejected:' : 'unhandled error:', err.message);
  if (res.headersSent) return res.end();
  if (misconfigured) {
    res.set('Retry-After', '120');
    return res.status(503).json({
      error: 'paid gates are unavailable',
      detail: 'The configured facilitator does not serve this network. No payment was taken.',
    });
  }
  return res.status(500).json({ error: 'internal error' });
});

app.listen(PORT, HOST, () => {
  console.log(`verify-api listening on ${HOST}:${PORT}`);
  void checkFacilitatorSupport();
});
