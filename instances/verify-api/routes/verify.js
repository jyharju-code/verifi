/**
 * Agent-facing two-gate Verify API handlers.
 *
 * Gate 1 admits a chain to the human queue for 0.10 USDC. Gate 2 unlocks
 * its ready result for 2.90 USDC. There is no free human work; an earned
 * failure credit can cover gate 1 of a later chain, but never gate 2.
 * Every POST returns a durable id and every result is retrieved by polling.
 */
import { Router } from 'express';
import crypto from 'node:crypto';

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

export const WALLET_RE = /^0x[0-9a-fA-F]{40}$/;
export const WALLET_ADDRESS_ERROR =
  'agent_id must be the requester wallet address (0x + 40 hex characters)';

export function isWallet(value) {
  return typeof value === 'string' && WALLET_RE.test(value);
}

/**
 * Decide what an identified wallet does before the x402 payment gate.
 *
 * Only checks that are cheaper than a payment challenge belong here. Queue
 * capacity is deliberately not one of them: answering a full queue with 503
 * before x402 means an unpaid caller never sees the 402 requirements, so
 * x402-aware clients and discovery crawlers cannot tell that this resource is
 * paid at all. Capacity is enforced by the core inside the admission
 * transaction instead, which is the only place it can be enforced correctly.
 */
export function admissionRoute(quota) {
  if (quota.pending_count > 0) return 'active-chain';
  const entitled = quota.entitlement_admission_available ?? quota.has_entry_entitlement;
  return entitled ? 'entitlement' : 'payment-gate';
}

export async function coreFetch(path, options = {}) {
  const headers = { 'content-type': 'application/json', ...(options.headers ?? {}) };
  if (CORE_INTERNAL_SECRET) headers['x-internal-secret'] = CORE_INTERNAL_SECRET;
  const resp = await fetch(`${CORE_API}${path}`, { ...options, headers });
  const body = await resp.json().catch(() => ({}));
  return { status: resp.status, body };
}

// ---------------------------------------------------------------------------
// Request audit. One durable row per /verify and /verify-unlock request, keyed
// by the nginx request id, written best-effort so it never breaks a response.
// Only safe, redacted fields leave this process: never a body, a claim or
// intent text, a full callback URL, or any secret header. The claim is reduced
// to its length and sha256 only.
// ---------------------------------------------------------------------------

function auditClientContext(req) {
  return {
    request_id: req.get('x-request-id') || null,
    client_ip: req.get('x-real-ip') || null,
    forwarded_for: (req.get('x-forwarded-for') || '').slice(0, 512) || null,
    forwarded_trusted: req.get('x-forwarded-trusted') === '1',
    user_agent: (req.get('user-agent') || '').slice(0, 512) || null,
    // Only the internal MCP server may claim source mcp; nginx forces rest on
    // every request that arrives through the public edge.
    source: req.get('x-verifi-source') === 'mcp' ? 'mcp' : 'rest',
  };
}

export function callbackHost(url) {
  if (typeof url !== 'string' || !url) return null;
  try { return new URL(url).hostname.slice(0, 255); } catch { return null; }
}

export function claimDigest(claim) {
  if (typeof claim !== 'string' || !claim) return { claim_len: null, claim_sha256: null };
  return {
    claim_len: claim.length,
    claim_sha256: crypto.createHash('sha256').update(claim).digest('hex'),
  };
}

export function outcomeFromStatus(status) {
  if (status === 202) return 'admitted';
  if (status === 200) return 'completed';
  if (status === 402) return 'payment_required';
  if (status === 429) return 'rejected_active';
  if (status === 503) return 'unavailable';
  if (status === 409) return 'conflict';
  if (status === 404) return 'not_found';
  if (status >= 500) return 'error';
  if (status >= 400) return 'bad_request';
  return 'ok';
}

export async function postRequestAudit(fields) {
  try {
    await coreFetch('/internal/request-audit', { method: 'POST', body: JSON.stringify(fields) });
  } catch (err) {
    console.error('request audit post failed:', err.message);
  }
}

// Express middleware. Attaches a finish hook that writes exactly one audit row
// once the final status is known. Handlers enrich req.auditContext with the
// fields only they can know (verify_id, funding, outcome, proof of ownership).
export function auditRequest(route) {
  return (req, res, next) => {
    const ctx = auditClientContext(req);
    // The MCP server may name the route after its tool, but only a genuine mcp
    // source may: nginx forces the source to rest on the public edge, so a
    // public caller cannot forge either the source or this route override.
    const effectiveRoute = (ctx.source === 'mcp' && req.get('x-verifi-route'))
      ? String(req.get('x-verifi-route')).slice(0, 200)
      : route;
    req.auditContext = { route: effectiveRoute, ...ctx };
    let written = false;
    const write = () => {
      if (written) return;
      written = true;
      const a = req.auditContext;
      const digest = a.claim_len != null
        ? { claim_len: a.claim_len, claim_sha256: a.claim_sha256 }
        : claimDigest(req.body?.claim);
      const bodyAgent = typeof req.body?.agent_id === 'string'
        ? req.body.agent_id.slice(0, 100) : null;
      postRequestAudit({
        source: a.source,
        request_id: a.request_id,
        route: a.route,
        client_ip: a.client_ip,
        forwarded_for: a.forwarded_for,
        forwarded_trusted: a.forwarded_trusted,
        user_agent: a.user_agent,
        agent_id: a.agent_id ?? bodyAgent,
        verify_id: a.verify_id ?? null,
        verify_no: a.verify_no ?? null,
        callback_host: a.callback_host ?? callbackHost(req.body?.callback_url),
        admission_source: a.admission_source ?? null,
        http_status: res.statusCode,
        payment_required: res.statusCode === 402,
        funding_state: a.funding_state ?? null,
        outcome: a.outcome ?? outcomeFromStatus(res.statusCode),
        // The x402 middleware verifies the payment signature before the route
        // runs, so an x402 admission is the only cryptographic proof that the
        // caller controls the wallet. A free-form agent_id is never proof.
        wallet_ownership_proven: a.wallet_ownership_proven ?? (req.admissionMode === 'x402'),
        claim_len: digest.claim_len,
        claim_sha256: digest.claim_sha256,
      });
    };
    res.once('finish', write);
    res.once('close', write);
    next();
  };
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
      const verifyId = getVerifyId();
      console.error(
        `SETTLEMENT NOT CAPTURED for verify ${verifyId}: check facilitator logs and reconcile`,
      );
      // Make the failure durable and dashboard-visible instead of leaving it
      // only in stderr. Best effort: if core is unreachable this also fails.
      const header = res.getHeader('PAYMENT-RESPONSE') ?? res.getHeader('payment-response');
      let transaction = null;
      try {
        if (header) transaction = JSON.parse(Buffer.from(String(header), 'base64').toString('utf8')).transaction ?? null;
      } catch { /* header unparseable: still alert without a tx */ }
      coreFetch('/internal/settlement-alerts', {
        method: 'POST',
        body: JSON.stringify({ verify_id: String(verifyId ?? ''), kind, transaction, detail: 'capture retries exhausted' }),
      }).catch((err) => console.error('settlement alert failed:', err.message));
    }
  };
  const start = () => attempt(7, 2_000);
  res.once('finish', start);
  res.once('close', start);
}

/**
 * Create one admission. Runs after the gate the caller had to pass, so it owns
 * every rejection that the preflight cannot make cheaply.
 *
 * Request validation lives here rather than before the payment gate because
 * x402 cancels settlement for any 4xx or 5xx this handler returns: the signed
 * authorization is never submitted to the facilitator, so a rejected request
 * still costs the caller nothing.
 */
export async function handleVerify(req, res) {
  const { intent, claim, agent_id: agentId, callback_url: callbackUrl } = req.body ?? {};
  if (!isWallet(agentId)) {
    return res.status(400).json({ error: WALLET_ADDRESS_ERROR });
  }
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
      request_id: req.auditContext?.request_id ?? null,
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
  // The core refuses a full queue before it inserts the row and before it
  // consumes an entitlement, so nothing is spent here. On the x402 path this
  // 503 also cancels settlement, so the entry payment is never submitted and
  // the caller keeps its 0.10 USDC. No credit or refund is needed.
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

  if (req.auditContext) {
    const entrySource = create.body.entry_source ?? null;
    req.auditContext.verify_id = create.body.id ?? null;
    req.auditContext.verify_no = create.body.verify_no ?? null;
    req.auditContext.admission_source = entrySource;
    req.auditContext.funding_state = req.admissionMode === 'x402'
      ? 'x402_paid'
      : (entrySource === 'failure_credit' ? 'credit' : 'entitlement');
    req.auditContext.outcome = 'admitted';
    req.auditContext.wallet_ownership_proven = req.admissionMode === 'x402';
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
