/**
 * Verify API: first Verifi instance.
 *
 * Route order matters: the first POST /verify route serves the free tier
 * while the per-address quota remains and hands off with next('route') when
 * it is exhausted; the second POST /verify route requires an x402 payment
 * through the self-hosted facilitator before the same handler runs.
 * POST /verify-unlock is the paid recovery path for a paid response whose
 * original settlement failed (unlock_paid = false).
 */
import express from 'express';
import { paymentMiddleware, x402ResourceServer } from '@x402/express';
import { ExactEvmScheme } from '@x402/evm/exact/server';
import { HTTPFacilitatorClient } from '@x402/core/server';
import {
  verifyRouter,
  freeRemaining,
  quotaFor,
  getVerify,
  handleVerify,
  recordSettlementOnFinish,
} from './routes/verify.js';

const PORT = Number(process.env.PORT ?? 8702);
const HOST = process.env.HOST ?? '127.0.0.1';
const X402_PAY_TO = process.env.X402_PAY_TO ?? '';
const X402_PRICE = process.env.X402_PRICE ?? '$0.10';
const X402_UNLOCK_PRICE = process.env.X402_UNLOCK_PRICE ?? '$3.00';
const X402_NETWORK = process.env.X402_NETWORK ?? 'eip155:8453';
const FACILITATOR_URL = process.env.FACILITATOR_URL ?? 'https://x402.org/facilitator';

const WALLET_RE = /^0x[0-9a-fA-F]{40}$/;
const UUID_RE = /^[0-9a-f-]{36}$/i;

const app = express();
// nginx terminates TLS and forwards the original scheme. Trust only the
// directly connected proxy so x402 advertises https:// resource URLs instead
// of the container's internal http:// URL.
app.set('trust proxy', 1);
app.use(express.json({ limit: '32kb' }));

app.get('/health', (_req, res) => res.json({ ok: true }));

// Free tier: 5 verifies per wallet address. The address is self-reported at
// this tier (there is no payment to prove it), which is an accepted
// limitation of a free allowance. Requests without a valid wallet-formatted
// agent_id fall through to the paid route.
app.post('/verify', async (req, res, next) => {
  try {
    const agentId = req.body?.agent_id;
    if (typeof agentId === 'string' && WALLET_RE.test(agentId)) {
      const quota = await quotaFor(agentId);
      // Refuse BEFORE the payment path so nobody pays only to hit 429.
      if (quota.pending_count > 0) {
        return res.status(429).json({
          error: 'one pending verify per agent_id',
          detail: 'Wait until your previous verify is answered or expires (60 min), then try again.',
        });
      }
      if (quota.free_remaining > 0) {
        req.verifyTier = 'free';
        return await handleVerify(req, res);
      }
    }
  } catch (err) {
    console.error('quota check failed, requiring payment:', err.message);
  }
  return next('route');
});

if (X402_PAY_TO) {
  const facilitatorClient = new HTTPFacilitatorClient({ url: FACILITATOR_URL });
  const resourceServer = new x402ResourceServer(facilitatorClient)
    .register(X402_NETWORK, new ExactEvmScheme());

  // Paid verify: $X402_PRICE, response auto-unlocks with this payment.
  app.post(
    '/verify',
    paymentMiddleware(
      {
        'POST /verify': {
          accepts: {
            scheme: 'exact',
            price: X402_PRICE,
            network: X402_NETWORK,
            payTo: X402_PAY_TO,
          },
          description: 'One human verification of a claim. A real person answers.',
        },
      },
      resourceServer,
    ),
    (req, res) => {
      req.verifyTier = 'paid';
      return handleVerify(req, res);
    },
  );

  // Unlock recovery: only meaningful for a paid verify whose settlement
  // failed after creation. Pre-checks run before the payment middleware so
  // nobody pays for an unlock that is impossible or unnecessary.
  app.post(
    '/verify-unlock',
    async (req, res, next) => {
      const id = String(req.query.id ?? '');
      if (!UUID_RE.test(id)) {
        return res.status(400).json({ error: 'pass the verify id as ?id=<uuid>' });
      }
      const { status, body } = await getVerify(id);
      if (status === 404) return res.status(404).json({ error: 'verify not found' });
      if (status !== 200) return res.status(502).json({ error: 'verification backend unavailable' });
      if (body.tier !== 'paid') {
        return res.status(409).json({ error: 'free tier responses are never locked' });
      }
      if (body.unlock_paid) {
        return res.status(409).json({ error: 'already unlocked' });
      }
      req.unlockVerifyId = id;
      return next();
    },
    paymentMiddleware(
      {
        'POST /verify-unlock': {
          accepts: {
            scheme: 'exact',
            price: X402_UNLOCK_PRICE,
            network: X402_NETWORK,
            payTo: X402_PAY_TO,
          },
          description: 'Unlock a paid verify response whose original payment did not settle.',
        },
      },
      resourceServer,
    ),
    (req, res) => {
      recordSettlementOnFinish(res, 'unlock', () => req.unlockVerifyId);
      return res.json({
        verify_id: req.unlockVerifyId,
        unlock_paid: true,
        message: `Unlocked. GET /verify/${req.unlockVerifyId} now returns the response.`,
      });
    },
  );

  console.log(
    `x402 paid tier active: ${X402_PRICE} per verify, ${X402_UNLOCK_PRICE} unlock, ` +
    `${X402_NETWORK} via ${FACILITATOR_URL}`,
  );
} else {
  app.post('/verify', (_req, res) => {
    res.status(503).json({ error: 'paid tier not configured and free tier exhausted' });
  });
  console.warn('X402_PAY_TO not set: paid tier disabled, only free tier works');
}

// Spec-compatible alias for the unlock path.
app.post('/verify/:id/unlock', (req, res) => {
  res.redirect(308, `/verify-unlock?id=${encodeURIComponent(req.params.id)}`);
});

app.use('/', verifyRouter);

app.listen(PORT, HOST, () => {
  console.log(`verify-api listening on ${HOST}:${PORT}`);
});
