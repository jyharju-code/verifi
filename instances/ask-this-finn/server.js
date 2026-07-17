/**
 * Ask This Finn: second Verifi instance. Placeholder.
 *
 * Per CLAUDE.md the build order is core engine, then Verify API, then ATF.
 * The full ATF design (x402 exact 2.22 USDC, ticket contract, attestation)
 * lives in the ask-this-finn repository and will be ported onto the core
 * engine when this instance is built.
 */
import express from 'express';
import { askRouter } from './routes/ask.js';

const PORT = Number(process.env.PORT ?? 8703);
const HOST = process.env.HOST ?? '127.0.0.1';

const app = express();
app.use(express.json({ limit: '32kb' }));

app.get('/health', (_req, res) => res.json({ ok: true }));
app.use('/', askRouter);

app.listen(PORT, HOST, () => {
  console.log(`ask-this-finn listening on ${HOST}:${PORT}`);
});
