/**
 * Proof that the caller controls the wallet it names, from a standard x402
 * payment signature, without settling the payment.
 *
 * A free admission (an earned failure credit, or a legacy free use) is tied
 * to a wallet address. Trusting the address alone let anyone who knew a
 * credited wallet spend its credit. Instead, a credited wallet goes through
 * the same 402 as everyone else, and the EIP-3009 authorization it signs is
 * checked here: the signer must be agent_id, the transfer must go to our
 * payTo for exactly the admission amount of a quote that is still valid, and
 * the authorization must be inside its validity window. When it holds, the
 * credit is used and the authorization is never submitted, so no funds move.
 *
 * Replay of an authorization seen on-chain is closed by the amount and quote
 * checks: a settled admission leaves its wallet with an active chain, which
 * is refused before this runs, and no other payment to Verifi carries the
 * admission amount of a currently valid quote.
 */
import { decodePaymentSignatureHeader } from '@x402/core/http';
import { recoverTypedDataAddress } from 'viem';
import { coreFetch } from './core.js';

const TRANSFER_WITH_AUTHORIZATION = [
  { name: 'from', type: 'address' },
  { name: 'to', type: 'address' },
  { name: 'value', type: 'uint256' },
  { name: 'validAfter', type: 'uint256' },
  { name: 'validBefore', type: 'uint256' },
  { name: 'nonce', type: 'bytes32' },
];

const lower = (s) => String(s ?? '').toLowerCase();

/** Returns { ok: true } or { ok: false, reason }. Never throws. */
export async function proveWallet(req, { agentId, payTo, network, now = () => Math.floor(Date.now() / 1000) }) {
  const header = req.get('payment-signature');
  if (!header) return { ok: false, reason: 'no payment signature' };
  let payload;
  try {
    payload = decodePaymentSignatureHeader(String(header));
  } catch {
    return { ok: false, reason: 'unreadable payment signature' };
  }
  const accepted = payload?.accepted;
  const auth = payload?.payload?.authorization;
  const signature = payload?.payload?.signature;
  if (!accepted || !auth || !signature) return { ok: false, reason: 'not an exact EIP-3009 payment' };
  if (accepted.network !== network) return { ok: false, reason: 'wrong network' };
  if (lower(auth.from) !== lower(agentId)) return { ok: false, reason: 'signer is not agent_id' };
  if (lower(auth.to) !== lower(payTo) || lower(accepted.payTo) !== lower(payTo)) {
    return { ok: false, reason: 'payment is not to Verifi' };
  }
  const t = now();
  if (!(Number(auth.validAfter) <= t && t < Number(auth.validBefore))) {
    return { ok: false, reason: 'authorization outside its validity window' };
  }

  // The authorization must be for gate 1 of a quote that can still be paid.
  const termsId = accepted.extra?.terms_id;
  if (typeof termsId !== 'string' || !termsId) return { ok: false, reason: 'no terms_id' };
  let quote;
  try {
    const r = await coreFetch(`/internal/terms/${encodeURIComponent(termsId)}`);
    if (r.status !== 200 || !r.body?.valid) return { ok: false, reason: 'quote unknown or expired' };
    quote = r.body;
  } catch {
    return { ok: false, reason: 'quote lookup failed' };
  }
  const price = quote.terms.prices.find((p) => lower(p.asset) === lower(accepted.asset));
  if (!price || price.network !== network) return { ok: false, reason: 'asset not in the quote' };
  if (String(auth.value) !== String(price.admission) || String(accepted.amount) !== String(price.admission)) {
    return { ok: false, reason: 'amount is not the admission price' };
  }

  // The signature itself: recover the signer over the token's EIP-712 domain.
  const chainId = Number(String(network).split(':')[1]);
  try {
    const signer = await recoverTypedDataAddress({
      domain: {
        name: accepted.extra?.name,
        version: accepted.extra?.version,
        chainId,
        verifyingContract: accepted.asset,
      },
      types: { TransferWithAuthorization: TRANSFER_WITH_AUTHORIZATION },
      primaryType: 'TransferWithAuthorization',
      message: {
        from: auth.from,
        to: auth.to,
        value: BigInt(auth.value),
        validAfter: BigInt(auth.validAfter),
        validBefore: BigInt(auth.validBefore),
        nonce: auth.nonce,
      },
      signature,
    });
    if (lower(signer) !== lower(agentId)) return { ok: false, reason: 'signature is not from agent_id' };
  } catch {
    return { ok: false, reason: 'invalid signature' };
  }
  return { ok: true };
}
