# Verifi Agent API

Contract version: 3

Base URL: `https://verifi.cloud`

Verifi routes a request from an AI agent to a real human. Human work can take
minutes or hours. The API is asynchronous and every accepted chain is
identified by a durable `verify_id` (also returned as `work_id`).

This file is the canonical human-readable API contract. When behavior changes,
update this file, `deploy/nginx/html/docs/index.html`, and
`deploy/nginx/html/llms.txt` in the same commit.

## Price and the three gates

Prices are set in euros and paid on Base mainnet in USDC or EURC. Every price
of a chain is known before the agent pays anything: the first `402` carries
the full terms.

| Gate | When | Price (EUR basis) |
| --- | --- | --- |
| 1. Admission | `POST /verify` | 0.10 |
| 2. SLA unlock | the human answered within 60 minutes of admission | 2.90 |
| 3. Grace unlock | the human answered later, within 24 hours of admission | 1.45 |

1. Gate 1 admits the request to the human queue. The request does not enter
   the queue until the admission settlement has been recorded. The windows
   start at that moment, `admitted_at`.
2. When the human answers, the unlock price is fixed by the time of the answer
   alone, `ready_at`: the SLA price if `ready_at` is within the SLA window,
   otherwise the grace price. A boundary instant belongs to the earlier,
   higher-priced window. When the agent polls or unlocks does not change the
   price.
3. `POST /verify-unlock?id={verify_id}` then costs exactly that one price. The
   result is released only after this settlement succeeds.

The asset of gate 1 binds the chain: gates 2 and 3 are paid in the same asset
on the same network. EURC amounts are the euro prices. USDC amounts are the
euro prices converted at the ECB euro reference rate and rounded up to the
next cent, at the moment of the quote; the conversion is disclosed in the
terms and never changes after admission. With 1 EUR = 1.17 USD, a USDC chain
costs 0.12, then 3.40 or 1.70.

Rates: ECB euro foreign exchange reference rates, published for information
purposes only. Source: European Central Bank.

Every verification is paid. Human time is the scarce resource, so there is no
free human work. What is free needs no human. The docs, MCP discovery, the
status endpoint, and the `402 Payment Required` response itself cost nothing,
so an agent can test the connection and read every price before paying.

There is no cancellation. Paying the admission is a commitment: a ready result
unlocks at the SLA price or at the grace price. A ready result stays
unlockable at its fixed price; there is currently no unlock deadline.

If no human answers within the last window (24 hours), the chain fails with
reason `human_timeout` and the wallet receives one admission credit: its next
admission is free. The credit is bound to the asset the expired chain was
paid in. It does not pay an unlock.

## Agent algorithm

1. Send `POST /verify` with `intent`, `claim`, and the requester wallet in
   `agent_id`.
2. If the server returns `402`, read the terms in
   `extensions["service-windows"]`, choose one of the `accepts` options (USDC
   is listed first, EURC second), complete the x402 payment, and repeat the
   same request. An earned admission credit, if the wallet has one, passes
   this gate without a payment.
3. Store the returned `verify_id` immediately. Prefer an HTTPS `callback_url`
   so the agent can yield until Verifi reports `ready` or `failed`.
4. If no callback is available, or callback delivery fails, poll
   `GET /verify/{verify_id}`. While status is `processing`, wait at least
   `retry_after_seconds` and poll again.
5. If status becomes `failed`, stop. Check `failure.entry_credit_granted`.
6. If status becomes `ready`, check `service_window`, then call
   `POST /verify-unlock?id={verify_id}` and pay the one requirement it
   returns.
7. Read the result only from a `completed` response.

Do not impose a short fixed client deadline. A chain can wait up to 24 hours
for its human and can be resumed later with the same `verify_id`.

## The terms in the first 402

`POST /verify` without a payment answers `402`. Its `PAYMENT-REQUIRED` header
decodes to a standard x402 v2 `PaymentRequired` with one `exact` option per
accepted asset, and the terms of the chain in the `service-windows` extension:

```json
{
  "x402Version": 2,
  "accepts": [
    { "scheme": "exact", "network": "eip155:8453",
      "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "amount": "120000",
      "payTo": "0x...", "maxTimeoutSeconds": 300,
      "extra": { "name": "USD Coin", "version": "2", "terms_id": "st_..." } },
    { "scheme": "exact", "network": "eip155:8453",
      "asset": "0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42", "amount": "100000",
      "payTo": "0x...", "maxTimeoutSeconds": 300,
      "extra": { "name": "EURC", "version": "2", "terms_id": "st_..." } }
  ],
  "extensions": {
    "service-windows": {
      "info": {
        "version": 1,
        "terms_id": "st_...",
        "terms_valid_until": "2026-10-01T09:10:00.000Z",
        "windows": { "sla": { "within_seconds": 3600 }, "grace": { "within_seconds": 86400 } },
        "price_basis": { "currency": "EUR", "admission": "0.10", "sla": "2.90", "grace": "1.45" },
        "prices": [
          { "network": "eip155:8453", "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "admission": "120000", "sla": "3400000", "grace": "1700000",
            "conversion": { "from": "EUR", "rate": "1.17", "source": "ECB euro reference rate",
                            "as_of": "2026-09-30", "rounding": "up_to_cent" } },
          { "network": "eip155:8453", "asset": "0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42",
            "admission": "100000", "sla": "2900000", "grace": "1450000" }
        ],
        "asset_binding": "admission_asset",
        "expiry": { "admission_credit": "next_admission" },
        "cancellation": "none",
        "status_url_template": "https://verifi.cloud/verify/{work_id}",
        "unlock_url_template": "https://verifi.cloud/verify-unlock?id={work_id}"
      },
      "schema": { "type": "object" }
    },
    "bazaar": { "info": { "input": { "type": "http", "method": "POST", "bodyType": "json" } } }
  }
}
```

Amounts in `accepts` and `prices` are strings in the asset's atomic units (6
decimals for both assets). For each asset, `prices[].admission` equals the
`amount` of its `accepts` option. A quote can be paid until
`terms_valid_until`, about ten minutes. A payment signed against an expired
quote is not settled: the answer is a fresh `402` with fresh terms, and the
agent signs again.

The `service-windows` extension follows the draft x402 extension in
`docs/specs/extension-service-windows.md`.

## POST /verify

JSON body:

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `intent` | string, max 2000 | yes | What the agent is trying to do |
| `claim` | string, max 4000 | yes | What the human should verify or answer |
| `agent_id` | `0x` wallet address | yes | Requester wallet and quota identity |
| `callback_url` | HTTPS URL | no | Optional ready or failed notification |

For unattended agents, `callback_url` is the recommended completion signal.
It removes the need for an active polling loop. Persist `verify_id` before
yielding so polling remains available as a recovery path.

An admitted request returns HTTP `202` with the terms bound to the chain:

```json
{
  "verify_id": "a1b2c3d4-0000-4000-8000-000000000000",
  "work_id": "a1b2c3d4-0000-4000-8000-000000000000",
  "contract_version": 3,
  "status": "processing",
  "next_action": "poll",
  "poll_url": "/verify/a1b2c3d4-0000-4000-8000-000000000000",
  "retry_after_seconds": 15,
  "admitted_at": null,
  "sla_deadline": null,
  "grace_deadline": null,
  "terms": {
    "terms_id": "st_...",
    "network": "eip155:8453",
    "asset": "0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42",
    "asset_symbol": "EURC",
    "admission": "100000", "sla": "2900000", "grace": "1450000",
    "windows": { "sla": { "within_seconds": 3600 }, "grace": { "within_seconds": 86400 } }
  },
  "funding": {
    "entry_source": "x402",
    "asset": { "network": "eip155:8453", "address": "0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42",
               "symbol": "EURC", "decimals": 6 },
    "entry_amount_atomic": "100000",
    "entry_charged_atomic": "100000",
    "unlock_amount_atomic": "2900000",
    "entry_charged_usdc": null
  }
}
```

The x402 response is built while the admission payment settles, so on the
`202` of a paid admission `admitted_at` and both deadlines are still `null`.
They appear on the first poll, and so does `expires_at`. An admission paid
with a credit has them at once.

The `*_usdc` funding fields keep their meaning on a USDC chain and are `null`
on an EURC chain, where a USDC number would be misleading. The atomic fields
are always present. Until the human answers, `unlock_amount_atomic` shows the
SLA amount, the most the unlock can cost.

Possible `entry_source` values are:

| Source | Gate 1 | Unlock |
| --- | --- | --- |
| `x402` | paid | paid, SLA or grace price |
| `failure_credit` | free | paid, SLA or grace price |
| `initial_free` | free | free (chains from before contract 3 only) |

## GET /verify/{verify_id}

This endpoint never costs money. Use it when no callback is available, or as
the recovery path if callback delivery fails. Continue until the status is
`ready`, `failed`, or `completed`.

| Status | Meaning | Required next action |
| --- | --- | --- |
| `processing` | Admission is settling or a human is working | Wait for callback, or poll to recover |
| `ready` | Human result exists but is locked | Call the unlock endpoint |
| `failed` | No redeemable result will be produced | Stop |
| `completed` | The unlock passed and the result is visible | Use the result |

A processing response carries `admitted_at`, `sla_deadline`, and
`grace_deadline`, so the agent can plan its polling. `expires_at` equals the
last deadline.

A ready response contains no verdict or answer, but it states exactly what the
unlock costs and why:

```json
{
  "verify_id": "a1b2c3d4-0000-4000-8000-000000000000",
  "work_id": "a1b2c3d4-0000-4000-8000-000000000000",
  "contract_version": 3,
  "status": "ready",
  "verdict": null,
  "explanation": null,
  "admitted_at": "2026-10-01T09:00:00.100Z",
  "ready_at": "2026-10-01T13:12:40.000Z",
  "service_window": {
    "applied": "grace",
    "sla_deadline": "2026-10-01T10:00:00.100Z",
    "grace_deadline": "2026-10-02T09:00:00.100Z",
    "network": "eip155:8453",
    "asset": "0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42",
    "unlock_amount": "1450000"
  },
  "next_action": "unlock",
  "unlock_url": "https://verifi.cloud/verify-unlock?id=a1b2c3d4-0000-4000-8000-000000000000",
  "unlock": {
    "method": "POST",
    "url": "/verify-unlock?id=a1b2c3d4-0000-4000-8000-000000000000",
    "price_usdc": null,
    "payment_required": true,
    "funded_by": "x402",
    "network": "eip155:8453",
    "asset": "0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42",
    "asset_symbol": "EURC",
    "amount_atomic": "1450000",
    "amount_decimal": "1.45",
    "applied_window": "grace"
  }
}
```

On a USDC chain `unlock.price_usdc` is the decimal USDC price, for example
`"3.40"`.

## POST /verify-unlock?id={verify_id}

Call only when the callback or polling reports `ready`. Before that the
endpoint answers `409` and asks for no payment.

The endpoint returns HTTP `402` with exactly one `exact` requirement: the
asset the admission was paid in, at `service_window.unlock_amount`. Its
`extensions["service-windows"].info` repeats `terms_id`, `applied`,
`admitted_at`, and `ready_at`, so the agent can check the arithmetic against
the terms it paid for at admission. Refuse to sign if the asset differs or
the amount is higher than the bound amount for the applied window. Sign it and
repeat the unlock request. An admission credit does not cover this gate.

The successful response has status `completed` and contains the human result:

```json
{
  "verify_id": "a1b2c3d4-0000-4000-8000-000000000000",
  "status": "completed",
  "human_status": "refined",
  "verdict": "refined",
  "explanation": "Use the corrected delivery date: 22 July.",
  "response": "Use the corrected delivery date: 22 July.",
  "next_action": "done",
  "funding": {
    "entry_charged_atomic": "100000",
    "unlock_window": "grace",
    "unlock_charged_atomic": "1450000",
    "total_charged_atomic": "1550000"
  }
}
```

A `completed` chain returns its result again without a further charge.

The human verdict vocabulary is `true`, `false`, or `refined`. A refined
free-text answer is in `explanation` and `response`.

## Failed chains and credits

A chain nobody answers within the last window returns:

```json
{
  "verify_id": "a1b2c3d4-0000-4000-8000-000000000000",
  "status": "failed",
  "next_action": "stop",
  "failure": {
    "reason": "human_timeout",
    "entry_credit_granted": true,
    "entry_credit": "next_admission",
    "entry_credit_value_usdc": null
  }
}
```

The public status stays `failed` with reason `human_timeout`, as in contract
2, so agents that stop on `failed` keep working. The credit is not a cash
transfer. It is an auditable entitlement that pays only the admission of the
wallet's next chain, which is then bound to the same asset.
`entry_credit_value_usdc` is the admission price on a USDC chain and `null`
on an EURC chain.

## x402 payment

All gates use x402 v2, scheme `exact`, on Base mainnet `eip155:8453`, with
USDC (`0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`) or EURC
(`0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42`). Both tokens support EIP-3009;
the EIP-712 domain name and version are in each requirement's `extra`. A
request without payment receives a base64 encoded `PAYMENT-REQUIRED` header.
Sign the EIP-3009 authorization for the chosen requirement and repeat the same
request with `PAYMENT-SIGNATURE`. The successful response contains
`PAYMENT-RESPONSE` with the settlement transaction. The required amount is the
requirement's `amount` field, in atomic units.

The admission payment and the unlock payment are distinct authorizations and
distinct on-chain transactions tied to the same `verify_id`.

### Automatic HTTP payment with `@x402/fetch`

Install `@x402/core`, `@x402/fetch`, `@x402/evm`, and `viem`. The wrapper reads
the `402` requirement, creates the EIP-3009 authorization with the requester
wallet, and retries the same HTTP request automatically. Without a custom
selector it pays with the first option, USDC:

```sh
npm install @x402/core @x402/fetch @x402/evm viem
```

```ts
import { wrapFetchWithPayment } from "@x402/fetch";
import { x402Client } from "@x402/core/client";
import { ExactEvmScheme } from "@x402/evm/exact/client";
import { privateKeyToAccount } from "viem/accounts";

const signer = privateKeyToAccount(
  process.env.EVM_PRIVATE_KEY as `0x${string}`
);
// To pay in EURC instead, pass a selector:
// new x402Client((_v, accepts) => accepts.find((a) => a.extra?.name === "EURC") ?? accepts[0])
const client = new x402Client();
client.register("eip155:*", new ExactEvmScheme(signer));
const paidFetch = wrapFetchWithPayment(fetch, client);

const response = await paidFetch("https://verifi.cloud/verify", {
  method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify({
    intent: "Review this customer update",
    claim: "Check the promise and write a safer version.",
    agent_id: signer.address,
    callback_url: "https://your-agent.example/verifi-events",
  }),
});

const verify = await response.json();
```

Use the same `paidFetch` for `POST /verify-unlock?id={verify_id}` after the
callback reports `ready`. See the official x402 buyer quickstart:
https://docs.x402.org/getting-started/quickstart-for-buyers

## MCP

The MCP endpoint is `https://verifi.cloud/mcp`. It exposes four tools:

| Tool | Purpose |
| --- | --- |
| `verify_claim` | Submit a request, optionally register a callback, and pass gate 1 |
| `get_verify` | Poll a request without payment |
| `unlock_verify` | Pay the SLA or grace unlock and receive the ready answer |
| `verifi_info` | Read service rules and the current terms |

An x402-aware MCP client handles a paid gate as follows:

1. Call `verify_claim` or `unlock_verify` normally.
2. The tool returns a standard x402 `PaymentRequired` result with
   `x402Version`, `accepts`, `extensions`, and the MCP tool resource.
3. The MCP payment client signs the selected requirement with the requester
   wallet and repeats the same call with `x402/payment` metadata.
4. Verifi forwards the signed authorization to the corresponding HTTP gate.
5. A settled call returns the result and `x402/payment-response` metadata.

The private key stays in the agent's wallet. Only the signed, single-use x402
authorization is sent through MCP. The admission and the unlock require
separate signatures and remain tied to the same `verify_id`. Generic MCP
clients that do not implement x402 metadata can pass the encoded
authorization through the optional `payment_signature` argument.

## Callback behavior

`callback_url` is optional and accepted by both HTTP `POST /verify` and MCP
`verify_claim`. Verifi sends `verify.ready` or `verify.failed` with the same
next-action fields used by polling, including `service_window` and the unlock
price on a ready chain. A ready callback never contains the locked result. A
callback replaces active polling during normal operation. Persist `verify_id`
and use polling only as the recovery path if delivery fails. Verifi makes up
to three callback delivery attempts. Treat the callback as a wake-up signal
and let the subsequent unlock action confirm the current state.

## Authentication

Public Verify API and MCP calls require no API key, signup, account, or
whitelist. The requester supplies a Base-compatible `0x` wallet address as
`agent_id`. That wallet signs the x402 payment authorizations and receives any
earned admission credits. The private key stays in the caller's wallet.

## Errors

| HTTP status | Meaning |
| --- | --- |
| `400` | Invalid input or wallet address |
| `402` | This gate requires an x402 payment, or the paid quote expired: sign the fresh terms |
| `409` | Wrong lifecycle action, for example unlock before ready, or the paid terms could not be bound |
| `429` | The wallet already has an active chain |
| `503` | Human queue is full, no quote can be issued right now, or paid routes are not configured. Retry after the advertised delay |

A refused admission request is never charged. The `429` for a wallet that
already has an active chain is answered before the payment gate. Every other
refusal is answered after it, and x402 cancels settlement for any `4xx` or
`5xx` response, so the signed authorization is never submitted to the
facilitator and no funds move. `POST /verify` therefore answers `402` with the
payment requirements whenever the caller has no entitlement, including while
the human queue is full and for a request that carries no valid `agent_id`.
The human queue keeps its hard cap; a full queue answers `503` after the gate.

## Audit model

PostgreSQL stores the complete chain:

1. `verifies` stores wallet address, request content, lifecycle timestamps,
   the bound terms, the applied window, charged amounts in the bound asset,
   funding sources, and both transaction hashes.
2. `pricing_terms` stores every quote, and `fx_rates` every ECB rate used.
3. `wallet_entitlements` stores every free use and failure credit, who
   received it, what it covers, its source chain and asset, and the chain
   that consumed it.
4. `audit_log` is append-only and records quotes, bindings, applied windows,
   settlements, ready results, failures, credits, callbacks, unlocks, and
   responder earnings.

The admin endpoint and dashboard expose these records to the operator.
