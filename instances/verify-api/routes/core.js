/**
 * The one way this instance talks to the core API.
 *
 * The core URL and secret are read per call rather than at import, so a test
 * can point the instance at a fake core it started after importing.
 */
export const INSTANCE = process.env.INSTANCE_ID ?? 'verify-api';

export async function coreFetch(path, options = {}) {
  const base = process.env.CORE_API_URL ?? 'http://127.0.0.1:8700';
  // Shared secret for the core money surface. Core rejects any /internal
  // call that does not carry it, so payment settlement cannot be forged even
  // if the core port becomes reachable. server.js refuses to start without it.
  const secret = process.env.CORE_INTERNAL_SECRET ?? '';
  const headers = {
    'content-type': 'application/json',
    ...(options.headers ?? {}),
    'x-internal-secret': secret,
  };
  const resp = await fetch(`${base}${path}`, { ...options, headers });
  const body = await resp.json().catch(() => ({}));
  return { status: resp.status, body };
}
