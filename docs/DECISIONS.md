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

## No free human work

Human time is the scarce resource, so no chain is free: every verification pays
the 0.10 USDC entry gate and the 2.90 USDC result gate. The earlier
five-free-chains-per-wallet allowance was removed (`verify-api.free_tier_count`
is `0`) because wallet addresses are free to mint, so an external script could
open five free human verifications per fresh address and flood the queue
without ever paying. What stays free needs no human: the docs, MCP discovery,
and the `402 Payment Required` response that lets an agent test the connection
and read the price. Note `FREE_DAILY_MAX=0` means unlimited, not zero, so the
allowance is switched off at the instance level, not through that budget. The
`initial_free` entitlement mechanism remains in the schema for historical
chains and so a future instance can re-enable it, but no new ones are granted.

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

The pending cap is deliberately enforced after the payment gate, not before
it. Answering a full queue with 503 first meant an unpaid caller never saw the
402 requirements, so x402 clients and discovery crawlers could not tell that
`POST /verify` is a paid resource at all: a busy hour looked like a broken
service. The cap is enforced by the core inside the admission transaction,
which is the only place it can be correct under concurrency anyway. Nobody
pays into that rejection either, because x402 cancels settlement for any 4xx
or 5xx the route returns. The same reasoning puts request validation after the
gate: a malformed `agent_id` gets 402 first, then 400, and is charged nothing.

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

## MCP covers free and paid chains

The MCP server reuses the public Verify API inside the compose network, so its
submit, poll, and unlock tools follow the same two-gate contract as HTTP. When
a paid gate is reached, the MCP tool returns the exact x402
`PAYMENT-REQUIRED` value. The agent signs it with its own wallet and repeats
the same tool with `payment_signature`. Verifi receives only the signed,
single-use authorization, never the agent's private key.

## Three gates, euro pricing, EURC (contract v3)

A human answer is worth more when it comes quickly, and an agent should know
every price before it pays anything. Contract v3 therefore has three gates:
an admission (0.10 EUR), and one unlock whose price is decided by when the
human answered: 2.90 EUR within the 60 minute SLA window, 1.45 EUR in the 24
hour grace window. The windows start at `admitted_at`, the recorded admission
settlement, so a slow settlement never eats into the SLA. The price depends
on `ready_at` alone, and a boundary instant belongs to the earlier window, so
an agent that polls slowly never loses the SLA price and one that polls
quickly never gains it. There is no cancellation: paying the admission is a
commitment to unlock at one of the two prices. After 24 hours without an
answer the chain fails and the wallet gets a free next admission.

Prices are defined once, in euros, by env (`ADMISSION_EUR`, `SLA_UNLOCK_EUR`,
`GRACE_UNLOCK_EUR`, `SLA_SECONDS`, `GRACE_SECONDS`) and validated at startup,
and one pricing function in `core/pricing.py` builds every quote. EURC pays
the euro prices. USDC pays them converted at the ECB euro reference rate,
rounded up to the next cent, with the rate, source and date disclosed. A
quote is stored and valid for ten minutes; the asset of the admission binds
the whole chain, and the bound amounts are never recomputed. The x402
requirement carries the quote id in `extra`, so a payment signed against an
expired quote or in the other asset matches nothing and is never settled.

USDC is listed first in `accepts` because x402 clients that do not choose pay
with the first option, and every agent built against contract 2 holds USDC.
Status `failed` with reason `human_timeout` is kept for an expired chain, so
those agents keep working. The terms are published in the `service-windows`
x402 extension, drafted in `docs/specs/extension-service-windows.md`.

Responders earn in the chain's own asset, never converted:
`floor(unlock_amount * commission / SLA price)`, with the commission
snapshotted at admission. An SLA answer earns the commission in full, a grace
answer proportionally less. The old `associates.earnings` total was carried
into the new ledger once as a USDC opening balance.

Open: a ready result can currently be unlocked at any time at its bound
price (no unlock deadline), and the human queue caps are unchanged although a
chain can now wait up to 24 hours.
