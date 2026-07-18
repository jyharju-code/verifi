/**
 * Agent-facing two-gate Verify API handlers.
 *
 * Gate 1 admits a chain to the human queue for 0.10 USDC. Gate 2 unlocks
 * its ready result for 2.90 USDC. The first five chains per wallet use one
 * full-free entitlement each, but still pass through both explicit gates.
 * Every POST returns a durable id and every result is retrieved by polling.
 */
import { Router } from 'express';

export const verifyRouter = Router();

const CORE_API = process.env.CORE_API_URL ?? 'http://127.0.0.1:8700';
const INSTANCE = process.env.INSTANCE_ID ?? 'verify-api';
// Shared secret for the core money surface. When set, core rejects any
// /internal call that does not carry it, so payment settlement cannot be
// forged even if the core port becomes reachable.
const CORE_INTERNAL_SECRET = process.env.CORE_INTERNAL_SECRET ?? '';
const EXPIRE_MS = 60 * 60 * 1000;
const RETRY_AFTER_S = 15;
const RESOLVED = new Set(['accepted', 'rejected', 'refined']);

export async function coreFetch(path, options = {}) {
  const headers = { 'content-type': 'application/json', ...(options.headers ?? {}) };
  if (CORE_INTERNAL_SECRET) headers['x-internal-secret'] = CORE_INTERNAL_SECRET;
  const resp = await fetch(`${CORE_API}${path}`, { ...options, headers });
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

export async function getVerify(id) {
  return coreFetch(`/internal/verifies/${id}`);
}

export function agentStatus(v, forceUnlocked = false) {
  if (v.status === 'admission_pending' || v.status === 'pending') return 'processing';
  if (v.status === 'expired' || v.status === 'failed') return 'failed';
  if (RESOLVED.has(v.status)) {
    return v.result_unlocked || forceUnlocked ? 'completed' : 'ready';
  }
  return 'processing';
}

export function publicView(v, { forceUnlocked = false } = {}) {
  const status = agentStatus(v, forceUnlocked);
  const unlocked = status === 'completed';
  const fullFree = v.entry_source === 'initial_free';
  const unlockSource = forceUnlocked ? 'x402' : v.unlock_source;
  const unlockCharged = forceUnlocked ? '2.90' : (v.unlock_charged_usdc ?? '0.00');
  const view = {
    verify_id: v.id,
    status,
    human_status: unlocked ? v.status : null,
    verdict: unlocked ? v.verdict ?? null : null,
    explanation: unlocked ? v.explanation ?? null : null,
    response: unlocked ? v.response : null,
    response_time_ms: unlocked ? v.response_time_ms : null,
    wallet_address: v.agent_id,
    funding: {
      entry_source: v.entry_source,
      free_use_number: v.free_use_number ?? null,
      entry_list_price_usdc: v.entry_list_price_usdc ?? '0.10',
      entry_charged_usdc: v.entry_charged_usdc ?? '0.00',
      unlock_source: unlockSource,
      unlock_list_price_usdc: v.unlock_list_price_usdc ?? '2.90',
      unlock_charged_usdc: unlockCharged,
      total_list_price_usdc: '3.00',
      total_charged_usdc: (
        Number(v.entry_charged_usdc ?? 0) + Number(unlockCharged)
      ).toFixed(2),
    },
    created_at: v.created_at,
    admitted_at: v.admitted_at,
    expires_at: v.expires_at,
    responded_at: v.responded_at,
    unlocked_at: v.unlocked_at,
  };

  if (status === 'processing') {
    view.next_action = 'poll';
    view.poll_url = `/verify/${v.id}`;
    view.retry_after_seconds = RETRY_AFTER_S;
  } else if (status === 'ready') {
    view.next_action = 'unlock';
    view.unlock = {
      method: 'POST',
      url: `/verify-unlock?id=${v.id}`,
      price_usdc: fullFree ? '0.00' : '2.90',
      payment_required: !fullFree,
      funded_by: fullFree ? 'initial_free' : 'x402',
    };
  } else if (status === 'failed') {
    view.next_action = 'stop';
    view.failure = {
      reason: v.failure_reason ?? (v.status === 'expired' ? 'human_timeout' : 'processing_failed'),
      entry_credit_granted: Boolean(v.failure_credit_granted),
      entry_credit_value_usdc: v.failure_credit_granted ? '0.10' : '0.00',
    };
  } else {
    view.next_action = 'done';
  }
  return view;
}

/**
 * The x402 middleware settles after the route has produced its buffered
 * response. The settlement header is available when finish fires. Capture
 * it with backoff so an aborted client connection cannot lose the ledger
 * record or prevent a settled entry from reaching the human queue.
 */
export function recordSettlementOnFinish(res, kind, getVerifyId) {
  let recorded = false;
  const attempt = async (retriesLeft, delayMs) => {
    if (recorded) return;
    try {
      const header = res.getHeader('PAYMENT-RESPONSE') ?? res.getHeader('payment-response');
      if (header) {
        const decoded = JSON.parse(Buffer.from(String(header), 'base64').toString('utf8'));
        const verifyId = getVerifyId();
        if (verifyId && decoded.transaction) {
          const result = await coreFetch(`/internal/verifies/${verifyId}/payment`, {
            method: 'POST',
            body: JSON.stringify({
              kind,
              transaction: decoded.transaction,
              payer: decoded.payer ?? null,
            }),
          });
          if (result.status === 200) {
            recorded = true;
            return;
          }
          throw new Error(`core payment record returned ${result.status}`);
        }
      }
    } catch (err) {
      console.error('settlement record failed:', err.message);
    }
    if (retriesLeft > 0) {
      setTimeout(() => attempt(retriesLeft - 1, Math.min(delayMs * 2, 60_000)), delayMs);
    } else if (!recorded) {
      console.error(
        `SETTLEMENT NOT CAPTURED for verify ${getVerifyId()}: check facilitator logs and reconcile`,
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

  const create = await coreFetch('/internal/verifies', {
    method: 'POST',
    body: JSON.stringify({
      instance: INSTANCE,
      intent: intent.trim(),
      claim: claim.trim(),
      agent_id: agentId,
      admission_mode: req.admissionMode,
      callback_url: callbackUrl ?? null,
    }),
  });
  if (create.status === 402) {
    return res.status(409).json({
      error: 'entry entitlement was consumed by another request',
      detail: 'Retry POST /verify. The next response will contain x402 payment requirements.',
    });
  }
  if (create.status === 429) {
    return res.status(429).json({
      error: 'one active verify per agent_id',
      detail: 'Poll the previous verify until it is completed or failed.',
    });
  }
  if (create.status === 503) {
    res.set('Retry-After', '120');
    return res.status(503).json({
      error: 'human queue is full',
      detail: 'Retry in a couple of minutes. No payment was taken.',
    });
  }
  if (create.status !== 200) {
    console.error('core create failed:', create.status, create.body);
    return res.status(502).json({ error: 'verification backend unavailable' });
  }

  if (req.admissionMode === 'x402') {
    const id = create.body.id;
    recordSettlementOnFinish(res, 'entry', () => id);
  }

  res.set('Retry-After', String(RETRY_AFTER_S));
  const admitted = req.admissionMode === 'x402'
    ? { ...create.body, entry_charged_usdc: '0.10' }
    : create.body;
  return res.status(202).json({
    ...publicView(admitted),
    response_timeout_ms: EXPIRE_MS,
    message: 'Admission accepted. Poll until status is ready or failed.',
  });
}

verifyRouter.get('/verify/:id', async (req, res) => {
  if (!/^[0-9a-f-]{36}$/i.test(req.params.id)) {
    return res.status(400).json({ error: 'invalid verify id' });
  }
  const { status, body } = await getVerify(req.params.id);
  if (status === 404) return res.status(404).json({ error: 'verify not found' });
  if (status !== 200) return res.status(502).json({ error: 'verification backend unavailable' });
  const view = publicView(body);
  if (view.status === 'processing') res.set('Retry-After', String(RETRY_AFTER_S));
  return res.json(view);
});
