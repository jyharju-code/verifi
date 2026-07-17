# Verifi Agent API

Contract version: 2

Base URL: `https://verifi.cloud`

Verifi routes a request from an AI agent to a real human. Human work can take
minutes. The API is asynchronous and every accepted chain is identified by a
durable `verify_id`.

This file is the canonical human-readable API contract. When behavior changes,
update this file, `deploy/nginx/html/docs/index.html`, and
`deploy/nginx/html/llms.txt` in the same commit.

## Price and the two gates

One successful paid chain costs exactly 3.00 USDC on Base:

1. Gate 1, entry: `POST /verify` costs 0.10 USDC. The request does not enter
   the human queue until this settlement has been recorded.
2. Gate 2, result: after polling reports `ready`,
   `POST /verify-unlock?id={verify_id}` costs a new 2.90 USDC payment. The
   result is released only after this settlement succeeds.

Every wallet receives five complete free chains. One free entitlement covers
both gate 1 and gate 2, but the agent still performs the separate submit, poll,
and unlock actions. Free and paid chains use the same queue, human workflow,
statuses, timing, callbacks, and response shapes.

If an admitted chain fails without a redeemable result, the wallet receives
one entry credit worth 0.10 USDC. The credit pays gate 1 of the next chain. It
does not pay the 2.90 USDC result gate.

## Agent algorithm

1. Send `POST /verify` with `intent`, `claim`, and the requester wallet in
   `agent_id`.
2. If the server returns `402`, complete the x402 payment and repeat the same
   request. A free entitlement or entry credit passes this gate without a
   payment.
3. Store the returned `verify_id` immediately.
4. Poll `GET /verify/{verify_id}`. While status is `processing`, wait at least
   `retry_after_seconds` and poll again.
5. If status becomes `failed`, stop. Check `failure.entry_credit_granted`.
6. If status becomes `ready`, call
   `POST /verify-unlock?id={verify_id}`. Complete a new x402 payment if the
   response is `402`. A full-free chain passes this gate without payment.
7. Read the result only from a `completed` response.

Do not impose a 110 second client deadline. Human work can take several
minutes. A chain can be resumed later with the same `verify_id` and expires
after 60 minutes if no human answers.

## POST /verify

JSON body:

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `intent` | string, max 2000 | yes | What the agent is trying to do |
| `claim` | string, max 4000 | yes | What the human should verify or answer |
| `agent_id` | `0x` wallet address | yes | Requester wallet and quota identity |
| `callback_url` | HTTPS URL | no | Optional ready or failed notification |

An admitted request returns HTTP `202`:

```json
{
  "verify_id": "a1b2c3d4-0000-4000-8000-000000000000",
  "status": "processing",
  "next_action": "poll",
  "poll_url": "/verify/a1b2c3d4-0000-4000-8000-000000000000",
  "retry_after_seconds": 15,
  "funding": {
    "entry_source": "x402",
    "entry_list_price_usdc": "0.10",
    "entry_charged_usdc": "0.10",
    "unlock_list_price_usdc": "2.90",
    "unlock_charged_usdc": "0.00",
    "total_list_price_usdc": "3.00",
    "total_charged_usdc": "0.10"
  }
}
```

Possible `entry_source` values are:

| Source | Gate 1 | Gate 2 |
| --- | --- | --- |
| `initial_free` | free | free |
| `failure_credit` | free | 2.90 USDC |
| `x402` | 0.10 USDC | 2.90 USDC |

## GET /verify/{verify_id}

This endpoint never costs money. Poll it until the status is `ready`, `failed`,
or `completed`.

| Status | Meaning | Required next action |
| --- | --- | --- |
| `processing` | Admission is settling or a human is working | Wait and poll |
| `ready` | Human result exists but is locked | Call the unlock endpoint |
| `failed` | No redeemable result will be produced | Stop |
| `completed` | Gate 2 passed and result is visible | Use the result |

A ready response contains no verdict or answer:

```json
{
  "verify_id": "a1b2c3d4-0000-4000-8000-000000000000",
  "status": "ready",
  "verdict": null,
  "explanation": null,
  "next_action": "unlock",
  "unlock": {
    "method": "POST",
    "url": "/verify-unlock?id=a1b2c3d4-0000-4000-8000-000000000000",
    "price_usdc": "2.90",
    "payment_required": true,
    "funded_by": "x402"
  }
}
```

## POST /verify-unlock?id={verify_id}

Call only when polling reports `ready`.

For a paid chain or an entry-credit chain, the endpoint returns HTTP `402`
with a new 2.90 USDC x402 requirement. Sign it and repeat the unlock request.
For one of the first five full-free chains, no payment is requested.

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
    "entry_charged_usdc": "0.10",
    "unlock_charged_usdc": "2.90",
    "total_charged_usdc": "3.00"
  }
}
```

The human verdict vocabulary is `true`, `false`, or `refined`. A refined
free-text answer is in `explanation` and `response`.

## Failed chains and credits

An unanswered chain expires after 60 minutes and returns:

```json
{
  "verify_id": "a1b2c3d4-0000-4000-8000-000000000000",
  "status": "failed",
  "next_action": "stop",
  "failure": {
    "reason": "human_timeout",
    "entry_credit_granted": true,
    "entry_credit_value_usdc": "0.10"
  }
}
```

The credit is not a cash transfer. It is an auditable entitlement that pays
only gate 1 of the wallet's next chain.

## x402 payment

Both paid gates use x402 v2, scheme `exact`, USDC on Base mainnet
`eip155:8453`. A request without payment receives a base64 encoded
`PAYMENT-REQUIRED` header. Sign the exact EIP-3009 authorization and repeat the
same request with `PAYMENT-SIGNATURE`. The successful response contains
`PAYMENT-RESPONSE` with the settlement transaction.

The entry payment and unlock payment are distinct authorizations and distinct
on-chain transactions tied to the same `verify_id`.

## Callback behavior

`callback_url` is optional. Verifi sends `verify.ready` or `verify.failed` with
the same next-action fields used by polling. A ready callback never contains
the locked result. Polling remains authoritative even if callback delivery
fails.

## Errors

| HTTP status | Meaning |
| --- | --- |
| `400` | Invalid input or wallet address |
| `402` | This gate requires an x402 payment |
| `409` | Wrong lifecycle action, for example unlock before ready |
| `429` | The wallet already has an active chain |
| `503` | Human queue is full or paid routes are not configured |

The server checks queue capacity before x402 middleware, so an admission
request rejected with `429` or `503` is not charged.

## Audit model

PostgreSQL stores the complete chain:

1. `verifies` stores wallet address, request content, lifecycle timestamps,
   list prices, charged amounts, funding sources, and both transaction hashes.
2. `wallet_entitlements` stores every full-free use and failure credit, who
   received it, what it covers, its source chain, and the chain that consumed
   it.
3. `audit_log` is append-only and records creation, entitlement consumption,
   settlements, ready results, failures, credits, callbacks, and unlocks.

The admin endpoint and dashboard expose these records to the operator.
