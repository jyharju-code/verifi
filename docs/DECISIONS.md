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

## Free tier is per wallet address

5 free verifies per `agent_id` (a wallet-formatted address), computed from
the verifies table, not from a global pool. The address is self-reported at
the free tier, which is an accepted limitation of a free allowance: rotating
addresses buys more free verifies but each one still faces the queue rules
below.

## Paid state follows settlement, not creation

The x402 Express middleware settles while the response is being finalized.
A paid verify is therefore created with `unlock_paid = false`, and the flag
flips only when the settlement transaction is recorded. This came from a
real incident: a client connection aborted mid-request and the settlement
landed seconds after the socket closed. Settlement capture listens to both
`finish` and `close` with retry backoff, and a missed capture logs loudly
for manual reconciliation.

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

## Expired paid verifies auto-credit

If a paid verify expires unanswered, money was taken without delivering.
Each expired paid verify permanently adds one free verify for that wallet
address. No refund transfers, no new tables: the credit is computed inside
the quota query.

## Unlock as a recovery path, not a product step

Paid responses unlock automatically when settlement lands. `unlock_paid`
can only be false when a settlement failed after creation, and
`POST /verify-unlock?id=...` (x402-paid) exists to recover exactly that
case. Pre-checks run before the payment middleware so nobody pays for an
impossible or unnecessary unlock. The canonical path uses a query parameter
because the x402 route matcher does not support path parameters.

## Webhooks with strict SSRF rules

Optional `callback_url` delivery on resolution or expiry: https only, port
443 only, hostname must resolve to public unicast addresses, redirects
disabled, three attempts with backoff. Polling always remains available, so
webhook failure never strands a result.

## Paid verifies settle before the human wait

The x402 Express middleware settles when the route ends its response. Holding
a paid response open while a human works can therefore charge the buyer after
the client connection has already closed, leaving the buyer without a
`verify_id`. Paid `POST /verify` calls always return `202` with a durable id as
soon as settlement completes. The human result is then retrieved with
`GET /verify/{id}` or delivered to `callback_url`. Free verifies may still use
the synchronous wait because no payment can be stranded.

## MCP surface covers the free tier

The MCP server reuses the public Verify API inside the compose network, so
quotas and caps apply identically. The x402-paid tier runs over plain HTTP;
x402-over-MCP can be added when agents ask for it.
