/**
 * Verify API process entry point. The app itself is built in app.js.
 */
import { createApp } from './app.js';

const PORT = Number(process.env.PORT ?? 8702);
const HOST = process.env.HOST ?? '127.0.0.1';

// x402 validates its routes against the facilitator in a promise created when
// the middleware is built, not when a request arrives. A facilitator that
// rejects the configured network, or that is simply unreachable at boot,
// therefore rejects a promise nothing is awaiting yet, and node kills the
// process for it. Free chains do not depend on the facilitator at all, so log
// loudly and keep serving instead of crash looping.
process.on('unhandledRejection', (reason) => {
  console.error('unhandled rejection, still serving:', reason?.message ?? reason);
});

// The core API rejects every /internal call without the shared secret, so a
// missing one would leave every verify failing at runtime. Fail at boot instead.
const MIN_SECRET_LENGTH = 32;
if ((process.env.CORE_INTERNAL_SECRET ?? '').length < MIN_SECRET_LENGTH) {
  console.error(
    `CORE_INTERNAL_SECRET must be set to at least ${MIN_SECRET_LENGTH} characters ` +
      '(generate one with: openssl rand -hex 24)',
  );
  process.exit(1);
}

const { app, checkFacilitatorSupport } = createApp();

app.listen(PORT, HOST, () => {
  console.log(`verify-api listening on ${HOST}:${PORT}`);
  void checkFacilitatorSupport();
});
