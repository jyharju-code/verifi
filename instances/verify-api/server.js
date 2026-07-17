/**
 * Verify API with two explicit gates for one verification chain.
 *
 * POST /verify costs 0.10 USDC and only then enters the human queue.
 * POST /verify-unlock costs 2.90 USDC after the result is ready. The first
 * five chains per wallet use a full-free entitlement at both gates.
 */
import express from 'express';
import { paymentMiddleware, x402ResourceServer } from '@x402/express';
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
} from './routes/verify.js';

const PORT = Number(process.env.PORT ?? 8702);
const HOST = process.env.HOST ?? '127.0.0.1';
const X402_PAY_TO = process.env.X402_PAY_TO ?? '';
const X402_ENTRY_PRICE = process.env.X402_ENTRY_PRICE ?? process.env.X402_PRICE ?? '$0.10';
const X402_UNLOCK_PRICE = process.env.X402_UNLOCK_PRICE ?? '$2.90';
const X402_NETWORK = process.env.X402_NETWORK ?? 'eip155:8453';
const FACILITATOR_URL = process.env.FACILITATOR_URL ?? 'https://x402.org/facilitator';

const WALLET_RE = /^0x[0-9a-fA-F]{40}$/;
const UUID_RE = /^[0-9a-f-]{36}$/i;

const app = express();
app.set('trust proxy', 1);
app.use(express.json({ limit: '32kb' }));

app.get('/health', (_req, res) => res.json({ ok: true }));

// Admission preflight is before both the entitlement path and x402. This
// prevents a payment if the wallet already has an active chain or the human
// capacity is full. The core repeats these checks transactionally.
app.post('/verify', async (req, res, next) => {
  try {
    const agentId = req.body?.agent_id;
    if (typeof agentId !== 'string' || !WALLET_RE.test(agentId)) {
      return res.status(400).json({
        error: 'agent_id must be the requester wallet address (0x + 40 hex characters)',
      });
    }
    const quota = await quotaFor(agentId);
    if (quota.pending_count > 0) {
      return res.status(429).json({
        error: 'one active verify per agent_id',
        detail: 'Poll the previous verify until it is completed or failed.',
      });
    }
    if (quota.queue_full) {
      res.set('Retry-After', '120');
      return res.status(503).json({
        error: 'human queue is full',
        detail: 'Retry in a couple of minutes. No payment was taken.',
      });
    }
    if (quota.has_entry_entitlement) {
      req.admissionMode = 'entitlement';
      return await handleVerify(req, res);
    }
    return next('route');
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

if (X402_PAY_TO) {
  const facilitatorClient = new HTTPFacilitatorClient({ url: FACILITATOR_URL });
  const resourceServer = new x402ResourceServer(facilitatorClient)
    .register(X402_NETWORK, new ExactEvmScheme());

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
  app.post('/verify', (_req, res) => {
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

app.listen(PORT, HOST, () => {
  console.log(`verify-api listening on ${HOST}:${PORT}`);
});
