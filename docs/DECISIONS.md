# Design decisions

The reasoning behind the non-obvious choices, in chronological order.

## One database, one VPS

Everything runs on a single VPS with PostgreSQL as the only state store:
ticketing, queueing, quotas, earnings, and the audit trail. A single operator
can understand, back up, and restore the whole system. Docker services bind to
localhost or the compose network; nginx is the only public surface.

## Self-hosted x402 facilitator

Hosted facilitators gate access by geography and company status. Verifi runs
its own [x402-rs](https://github.com/x402-rs/x402-rs) facilitator instead:
no permission needed, works everywhere. The facilitator verifies EIP-3009
payment authorizations and submits settlements on Base, paying gas from a
small dedicated gas wallet. Revenue settles directly from the buyer to the
operator's receiving address; the facilitator never holds funds. See
[WALLETS.md](WALLETS.md).

## Five full-free chains per wallet

Each wallet receives five complete free chains. One `initial_free` entitlement
covers both the 0.10 USDC entry gate and the 2.90 USDC result gate. It does not
bypass either lifecycle action: free agents submit, poll, and unlock with the
same statuses and response shape as paid agents. Consumption is stored in
`wallet_entitlements`, including the wallet, free-use number, and verify id.

## Admission follows settlement, not creation

The x402 Express middleware settles while the response is being finalized.
A paid verify is first persisted as `admission_pending`. It does not enter the
human queue until the 0.10 USDC transaction is recorded. This came from a real
incident: a client connection aborted mid-request and the settlement landed
seconds after the socket closed. Settlement capture listens to both `finish`
and `close` with retry backoff, and a missed capture logs loudly for manual
reconciliation.

## Structured verdicts without invented confidence

Agents need parseable output: `verdict` is `"true" | "false" | "refined"`,
mapped from the human's button press, with refine text in `explanation`.
There is deliberately no confidence number: a single human answering does
not produce a calibrated probability, and inventing one would be exactly
the kind of hallucination this service exists to prevent.

## Queue protection

Humans are the scarce resource, so three mechanisms protect them:
one pending verify per `agent_id` (429, checked before the payment path so
nobody pays into a rejection), a global pending cap (503 + Retry-After),
and a 60 minute expiry per verify.

## Failed admitted verifies grant entry credit

If an admitted verify expires or otherwise fails without a redeemable result,
the wallet receives one `failure_credit` entitlement worth 0.10 USDC. It pays
only the next entry gate. It never pays the 2.90 USDC result gate. The ledger
links the credit to both its failed source verify and its consuming verify.

## Unlock is the second product gate

Every ready response is locked. `POST /verify-unlock?id=...` is a required
second action. Paid and entry-credit chains settle a new 2.90 USDC x402
payment. A full-free chain uses the same endpoint but consumes the second half
of its original entitlement for 0.00 USDC. Pre-checks run before payment so no
one pays for a pending, failed, or already completed result.

## Webhooks with strict SSRF rules

Optional `callback_url` delivery on resolution or expiry: https only, port
443 only, hostname must resolve to public unicast addresses, redirects
disabled, three attempts with backoff. Polling always remains available, so
webhook failure never strands a result.

## All verifies are asynchronous and pollable

Holding a request open assumes a human deadline that the product cannot
promise. Every admitted `POST /verify` returns `202` with a durable id. Agents
poll `GET /verify/{id}` through `processing`, `ready`, `failed`, and
`completed`. `retry_after_seconds` controls polling cadence. A callback is an
optional ready or failed notification, never the only delivery path.

## MCP surface covers full-free chains

The MCP server reuses the public Verify API inside the compose network. Its
submit, poll, and unlock tools cover the five full-free chains. Paid x402 gates
run over plain HTTP; x402-over-MCP can be added when agents ask for it.
