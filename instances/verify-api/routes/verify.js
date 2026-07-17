/**
 * Verify API handlers.
 *
 * Free verifies can wait synchronously up to VERIFY_WAIT_TIMEOUT_S. Paid
 * verifies always return 202 immediately after x402 settlement so the buyer
 * receives a durable verify id before any human work begins. The agent then
 * polls GET /verify/:id or uses callback_url.
 *
 * Visibility: free tier responses are always open. Paid tier responses
 * auto-unlock with the $ per-verify payment; unlock_paid can only be false
 * if settlement failed after creation, and then the unlock endpoint is the
 * recovery path.
 */
import { Router } from 'express';

export const verifyRouter = Router();

const CORE_API = process.env.CORE_API_URL ?? 'http://127.0.0.1:8700';
const INSTANCE = process.env.INSTANCE_ID ?? 'verify-api';
const WAIT_S = Number(process.env.VERIFY_WAIT_TIMEOUT_S ?? 110);
const EXPIRE_MS = 60 * 60 * 1000;

async function coreFetch(path, options = {}) {
  const resp = await fetch(`${CORE_API}${path}`, {
    headers: { 'content-type': 'application/json' },
    ...options,
  });
  const body = await resp.json().catch(() => ({}));
  return { status: resp.status, body };
}

export async function quotaFor(agentId) {
  const { status, body } = await coreFetch(
    `/internal/quota?instance=${INSTANCE}&agent_id=${encodeURIComponent(agentId)}`,
  );
  if (status !== 200) throw new Error(`core quota returned ${status}`);
  return body;
}

export async function freeRemaining(agentId) {
  if (!agentId) return 0;
  return (await quotaFor(agentId)).free_remaining;
}

export async function getVerify(id) {
  return coreFetch(`/internal/verifies/${id}`);
}

export function publicView(v) {
  const locked = v.tier === 'paid' && !v.unlock_paid;
  const view = {
    verify_id: v.id,
    status: v.status,
    verdict: locked ? null : v.verdict ?? null,
    explanation: locked ? null : v.explanation ?? null,
    response: locked ? null : v.response,
    response_time_ms: locked ? null : v.response_time_ms,
    tier: v.tier,
    unlock_paid: v.unlock_paid,
    created_at: v.created_at,
    expires_at: v.expires_at,
    responded_at: v.responded_at,
  };
  if (locked && v.status !== 'pending') {
    view.unlock_hint = `Response is locked because the original payment did not settle. Pay the unlock price via POST /verify-unlock?id=${v.id}`;
  }
  return view;
}

export function effectiveWaitSeconds(tier, requestedWaitS) {
  // @x402/express settles only when the route ends the response. Waiting for
  // a human before ending a paid response can strand the buyer if the HTTP
  // connection closes while settlement still succeeds. Settle first, return
  // the durable id, then deliver the human result asynchronously.
  return tier === 'paid' ? 0 : requestedWaitS;
}

/**
 * After the x402 middleware settles (it finalizes during the response), the
 * PAYMENT-RESPONSE header carries the settlement transaction. Record it on
 * the verify so every payment has an on-chain reference in the database.
 */
export function recordSettlementOnFinish(res, kind, getVerifyId) {
  let recorded = false;
  // The middleware settles while the response is being finalized, and with
  // an aborted connection the settlement can land seconds AFTER close. Poll
  // the header with backoff so the transaction is captured in both cases.
  const attempt = async (retriesLeft, delayMs) => {
    if (recorded) return;
    try {
      const header = res.getHeader('PAYMENT-RESPONSE') ?? res.getHeader('payment-response');
      if (header) {
        const decoded = JSON.parse(Buffer.from(String(header), 'base64').toString('utf8'));
        const verifyId = getVerifyId();
        if (verifyId && decoded.transaction) {
          recorded = true;
          await coreFetch(`/internal/verifies/${verifyId}/payment`, {
            method: 'POST',
            body: JSON.stringify({
              kind,
              transaction: decoded.transaction,
              payer: decoded.payer ?? null,
            }),
          });
          return;
        }
      }
    } catch (err) {
      console.error('settlement record failed:', err.message);
    }
    if (retriesLeft > 0) {
      setTimeout(() => attempt(retriesLeft - 1, Math.min(delayMs * 2, 60_000)), delayMs);
    } else if (!recorded) {
      console.error(
        `SETTLEMENT NOT CAPTURED for verify ${getVerifyId()}: check facilitator logs and record manually`,
      );
    }
  };
  const start = () => attempt(7, 2_000);
  res.once('finish', start);
  res.once('close', start);
}

export async function handleVerify(req, res) {
  const { intent, claim, agent_id: agentId, callback_url: callbackUrl } = req.body ?? {};
  if (typeof intent !== 'string' || !intent.trim() || typeof claim !== 'string' || !claim.trim()) {
    return res.status(400).json({ error: 'intent and claim are required strings' });
  }
  if (intent.length > 2000 || claim.length > 4000) {
    return res.status(400).json({ error: 'intent max 2000 chars, claim max 4000 chars' });
  }
  if (callbackUrl !== undefined && callbackUrl !== null) {
    if (typeof callbackUrl !== 'string' || !callbackUrl.startsWith('https://') || callbackUrl.length > 2048) {
      return res.status(400).json({ error: 'callback_url must be an https URL, max 2048 chars' });
    }
  }
  // Free-tier agents can shorten the synchronous window with ?wait=<seconds>
  // (0..110). Paid requests always use zero so x402 settles before the human
  // wait and the buyer reliably receives a verify id.
  let waitS = WAIT_S;
  if (req.query.wait !== undefined) {
    const parsed = Number(req.query.wait);
    if (!Number.isFinite(parsed) || parsed < 0 || parsed > WAIT_S) {
      return res.status(400).json({ error: `wait must be 0..${WAIT_S} seconds` });
    }
    waitS = Math.floor(parsed);
  }

  const tier = req.verifyTier === 'free' ? 'free' : 'paid';
  waitS = effectiveWaitSeconds(tier, waitS);
  const create = await coreFetch('/internal/verifies', {
    method: 'POST',
    body: JSON.stringify({
      instance: INSTANCE,
      intent: intent.trim(),
      claim: claim.trim(),
      agent_id: typeof agentId === 'string' ? agentId.slice(0, 100) : null,
      tier,
      callback_url: callbackUrl ?? null,
    }),
  });
  if (create.status === 429) {
    return res.status(429).json({
      error: 'one pending verify per agent_id',
      detail: 'Wait until your previous verify is answered or expires (60 min), then try again.',
    });
  }
  if (create.status === 503) {
    res.set('Retry-After', '120');
    return res.status(503).json({
      error: 'human queue is full',
      detail: 'Too many verifies are waiting for humans right now. Retry in a couple of minutes.',
    });
  }
  if (create.status !== 200) {
    console.error('core create failed:', create.status, create.body);
    return res.status(502).json({ error: 'verification backend unavailable' });
  }

  if (tier === 'paid') {
    const id = create.body.id;
    recordSettlementOnFinish(res, 'payment', () => id);
  }

  if (waitS === 0) {
    return res.status(202).json({
      ...publicView(create.body),
      status: 'pending',
      response_timeout_ms: EXPIRE_MS,
      message: `A human is on it. Poll GET /verify/${create.body.id}`,
    });
  }
  const { body: resolved } = await coreFetch(
    `/internal/verifies/${create.body.id}/wait?timeout_s=${waitS}`,
  );
  if (resolved.status && resolved.status !== 'pending') {
    return res.json(publicView(resolved));
  }
  return res.status(202).json({
    ...publicView(create.body),
    status: 'pending',
    response_timeout_ms: EXPIRE_MS,
    message: `A human is on it. Poll GET /verify/${create.body.id}`,
  });
}

verifyRouter.get('/verify/:id', async (req, res) => {
  if (!/^[0-9a-f-]{36}$/i.test(req.params.id)) {
    return res.status(400).json({ error: 'invalid verify id' });
  }
  const { status, body } = await getVerify(req.params.id);
  if (status === 404) return res.status(404).json({ error: 'verify not found' });
  if (status !== 200) return res.status(502).json({ error: 'verification backend unavailable' });
  return res.json(publicView(body));
});
