# Extension: `service-windows`: three gates for asynchronous human work

- **Status:** Draft for discussion (not yet submitted)
- **Author:** Juhana Harju
- **Reference implementation:** Verifi, https://verifi.cloud (two-gate contract v2 live; three-gate contract v3 planned)
- **Type:** Protocol extension. It moves no money in a new way. Every gate settles with an existing scheme (`exact` is normative in this draft).
- **Intended location:** `specs/extensions/extension-service-windows.md`

## Summary

x402 assumes the resource exists when the payment arrives: pay, receive. Some
resources are produced by people. A person needs focus, has a queue, and sleeps.
Forcing human work into a synchronous request-response pattern either forces the
seller to promise an answer it cannot guarantee, or turns the human into a 24/7
machine.

This extension defines three gates that let an agent buy human work on terms both
sides can reason about:

1. **Admission gate.** A small payment admits the request to the human queue. It
   filters spam and denial-of-service traffic, and tells the human that the
   contact is worth their attention.
2. **Service-window gate.** A clearly stated service level (the SLA window) with
   a full price. A result that becomes ready inside the window is unlocked at that
   price.
3. **Grace-window gate.** The long tail. A result that becomes ready after the SLA
   window but inside a longer, minimum service level is unlocked at a stated
   discount. Human latency is priced in rather than treated as failure.

After the grace window, the work expires and no result can be bought.

All window lengths and prices are stated before the agent pays anything, fixed for
that work item at admission, and exposed in machine-readable form. Window lengths
and prices are deployment parameters: a single expert might offer a 60-minute SLA
and a 24-hour grace window, while a staffed pool of 100 people on 24/7 duty might
offer 10 minutes and 30 minutes.

The goal is a shared, standard vocabulary for the coexistence of agents and
people: agents get predictable terms, and people keep a human pace.

## Motivation

1. **No free human work.** Wallet addresses cost nothing to mint, so any free
   tier lets a script flood a human queue. The admission gate gives every request
   a cost, however small.
2. **Pay only for a redeemable result.** An agent should not prepay the full price
   for work that may never be produced. The main price is charged only when a
   result exists.
3. **Price follows the promise.** If the seller misses its stated service level,
   the price falls automatically, without a dispute process.
4. **Human rhythm is a feature.** The grace window makes it legitimate for a
   person to answer tomorrow morning. The seller discloses it and the buyer
   accepts it up front.
5. **Interoperability.** Today each seller of asynchronous work invents its own
   lifecycle. A shared extension lets agent frameworks implement one client flow
   for all of them.

Existing specifications cover adjacent ground but not this: `exact` and `upto`
price a single request; `auth-capture` holds and captures funds; the
`offer-and-receipt` extension signs what was offered and what was paid; and
proposals such as `x402-signals` (issue #2291) and the delivery-receipt work
(issue #2833) address fulfilment obligations and proof of delivery. None defines a
time-dependent price for a result that does not exist yet at payment time.

## Terminology

- **Work item:** one unit of requested human work, identified by a durable
  server-issued `work_id`.
- **Admitted at (`admitted_at`):** the time the admission payment settled and the
  work item entered the queue. All windows are measured from this instant, not
  from request creation.
- **Ready at (`ready_at`):** the time a human result was recorded by the server.
- **SLA window:** `[admitted_at, admitted_at + sla.within_seconds]`.
- **Grace window:** `(admitted_at + sla.within_seconds, admitted_at + grace.within_seconds]`.
- **Expired:** no result recorded by the end of the last window.
- **Unlock:** the payment that releases a ready result to the buyer.
- **Price basis:** the currency in which the seller defines its prices. Amounts
  in each accepted asset are derived from it.

The key words "MUST", "MUST NOT", "SHOULD", "SHOULD NOT" and "MAY" are to be
interpreted as described in RFC 2119.

## Lifecycle

```
          Gate 1                 Gate 2 (SLA)             Gate 3 (grace)
POST ──402──► pay admission ──► processing ──ready──► unlock at full price
                                    │
                                    ├─ready after SLA─► unlock at grace price
                                    │
                                    └─no result by end─► expired (no unlock)
```

States: `processing`, `ready`, `completed`, `expired`. A server MAY expose
additional failure states, for example when the admission settlement never
arrives.

## Gate 1: Admission

### Advertising the terms

The first unpaid request to the work resource MUST be answered with
`402 Payment Required`. `PaymentRequired.accepts` carries one admission payment
option per accepted asset (see `PaymentRequirements` in
`x402-specification-v2.md`). `PaymentRequired.extensions["service-windows"]` MUST
carry the full terms of the work item for every accepted asset, so the agent knows
every later price before it pays anything.

Example: prices defined in euros, payable in EURC or in USDC converted at the
quote-time rate (here 1 EUR = 1.17 USD, rounded up to the next cent).

```json
{
  "x402Version": 2,
  "accepts": [
    {
      "scheme": "exact",
      "network": "eip155:8453",
      "asset": "0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42",
      "amount": "100000",
      "payTo": "0x…",
      "resource": "https://verifi.cloud/verify"
    },
    {
      "scheme": "exact",
      "network": "eip155:8453",
      "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
      "amount": "120000",
      "payTo": "0x…",
      "resource": "https://verifi.cloud/verify"
    }
  ],
  "extensions": {
    "service-windows": {
      "info": {
        "version": 1,
        "terms_id": "c2b4…",
        "terms_valid_until": "2026-10-01T09:05:00Z",
        "windows": {
          "sla":   { "within_seconds": 3600 },
          "grace": { "within_seconds": 86400 }
        },
        "price_basis": {
          "currency": "EUR",
          "admission": "0.10",
          "sla": "2.90",
          "grace": "1.45"
        },
        "prices": [
          {
            "network": "eip155:8453",
            "asset": "0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42",
            "admission": "100000",
            "sla": "2900000",
            "grace": "1450000"
          },
          {
            "network": "eip155:8453",
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "admission": "120000",
            "sla": "3400000",
            "grace": "1700000",
            "conversion": {
              "from": "EUR",
              "rate": "1.17",
              "source": "ECB euro reference rate",
              "as_of": "2026-09-30",
              "rounding": "up_to_cent"
            }
          }
        ],
        "asset_binding": "admission_asset",
        "expiry": { "admission_credit": "next_admission" },
        "cancellation": "none",
        "status_url_template": "https://verifi.cloud/verify/{work_id}",
        "unlock_url_template": "https://verifi.cloud/verify-unlock?id={work_id}"
      },
      "schema": { "$comment": "JSON Schema for info; see Appendix A" }
    }
  }
}
```

Rules:

- Amounts in `prices` MUST be strings in each asset's atomic units, as in core
  x402 v2. Amounts in `price_basis` are decimal strings in the basis currency.
- For each asset, the `admission` amount MUST equal the `amount` of the matching
  `accepts` entry.
- `grace.within_seconds` MUST be greater than `sla.within_seconds`. For each
  asset, `grace` MUST be less than or equal to `sla`.
- A server that offers no grace window MUST omit `grace` everywhere. The work item
  then expires at the end of the SLA window.
- `terms_valid_until` bounds how long this quote may be accepted. After it, the
  server MUST issue a fresh 402 with fresh terms. A server that converts amounts
  between currencies SHOULD keep this interval short.

### Price basis and conversion

A server MAY define its prices in one basis currency and accept several assets.

- When an accepted asset is not denominated in the basis currency, the server
  MUST state how the amount was derived in `conversion` (rate, source, date and
  rounding).
- The conversion happens at quote time. The amounts quoted for the admission asset
  MUST then be bound to the work item. Later exchange-rate movements MUST NOT
  change them.
- `price_basis` is informative for clients. The amounts in `prices` are
  normative.

### Binding the terms

Terms accepted at admission MUST apply to the whole work item. When the admission
payment settles, the server MUST record, together with `admitted_at`:

- the windows,
- the asset the admission was paid in, and that asset's `sla` and `grace` amounts,
- the cancellation and expiry terms.

The server MUST NOT change any of these afterwards.

With `asset_binding: "admission_asset"`, every later gate of the work item MUST be
paid in the same asset on the same network as the admission. This draft defines
no other binding.

A server SHOULD sign the terms with the `offer-and-receipt` extension, so the
buyer can later prove what was promised. When it does, the signed offer MUST
include the `service-windows.info` object, or its digest.

### Cancellation

The terms MUST declare whether the buyer can cancel a work item:

- `none`: paying the admission is a commitment. The buyer accepts in advance that
  a ready result is unlocked at either the SLA amount or the grace amount. There
  is no cancellation and no credit for cancelling.
- `after_sla_admission_credit`: once the SLA window has passed without a result,
  the buyer MAY cancel and receives an admission credit for its next work item.
  The human work stops.

Verifi uses `none`.

### Admission outcomes

- A settled admission returns `202 Accepted` with `work_id`, `admitted_at`, the
  deadline of each window, and the bound terms.
- A request refused after the payment gate (queue full, invalid input) MUST NOT be
  charged. Under x402 HTTP semantics, a 4xx or 5xx answer cancels settlement.
- The paywall MUST run before request validation, so a cold probe always receives
  the 402 with the terms.

## Gates 2 and 3: Unlock

### Price determination

When a result becomes ready, the server MUST determine the unlock price from
`ready_at` alone:

| `ready_at − admitted_at` | Window | Unlock amount (bound asset) |
|---|---|---|
| `≤ sla.within_seconds` | SLA | the bound `sla` amount |
| `> sla.within_seconds` and `≤ grace.within_seconds` | grace | the bound `grace` amount |
| beyond the last window | none | not unlockable; state `expired` |

- The time at which the agent unlocks MUST NOT affect the price. An agent that
  polls slowly MUST NOT lose the SLA price, and one that polls quickly MUST NOT
  gain it.
- A boundary instant belongs to the earlier, higher-priced window.
- The server's clock is authoritative. The server MUST expose `admitted_at`,
  `ready_at` and the applied window, so the buyer can check the arithmetic.

### Status resource

`GET` on the status URL MUST be free and MUST NOT reveal a locked result. While
`ready`, the response MUST include:

```json
{
  "work_id": "0f3f7d0a-…",
  "status": "ready",
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
  "unlock_url": "https://verifi.cloud/verify-unlock?id=0f3f7d0a-…"
}
```

### Unlock payment

The unlock resource answers `402` with a single `exact` requirement in the bound
asset, whose `amount` equals `service_window.unlock_amount`.
`PaymentRequired.extensions["service-windows"].info` MUST repeat `applied`,
`admitted_at`, `ready_at` and `terms_id`.

A client:

- MUST compare the unlock amount and asset with the terms bound at admission, and
  MUST refuse to sign if the asset differs or the amount is higher than the bound
  amount for the applied window.
- SHOULD recompute the applied window from the exposed timestamps.

A server:

- MUST NOT request an unlock payment before the result is ready.
- MUST return the result without a further charge if the work item is already
  `completed`.
- MUST release the result only after the unlock settlement succeeds.

### Expiry

At the end of the last window without a ready result, the work item becomes
`expired` and no unlock is possible. The admission payment is not refunded
on-chain. The server SHOULD compensate it according to `expiry.admission_credit`:

- `next_admission`: the same payer's next admission is free (the Verifi behaviour).
- `none`: no compensation. The server MUST disclose this in the terms.

The admission payment compensates for queue protection and the human's attention,
not for a result, so a full refund is not required.

## Dynamic pricing

Every gate MAY be priced dynamically, for example by queue length, time of day,
pool size, or exchange rate. Dynamic pricing MUST NOT break the guarantee that all
prices are known before the agent pays:

- Prices and window lengths MAY change between quotes.
- Within a work item, the windows and the amounts for the bound asset MUST be
  those quoted in the admission 402 the agent paid against.
- A server using dynamic pricing SHOULD state its bounds in discovery metadata,
  for example Bazaar metadata or an OpenAPI `x-payment-info` price of
  `mode: "dynamic"` with `min` and `max`.

## Transport notes

- **HTTP:** as above. The admission and unlock resources are separate URLs, each
  with its own 402.
- **MCP:** a paid tool call returns the x402 `PaymentRequired` result per
  `transports-v2/mcp.md`, with the same `extensions["service-windows"]` object.
  Submit, status and unlock map to three tools.
- **Callbacks:** a server MAY accept a callback URL and notify `ready` or `expired`.
  A notification MUST NOT carry the locked result. Polling MUST remain available.

## Discovery

A server SHOULD advertise its windows and price basis in discovery metadata, so
agents can choose a seller by service level before any request. With Bazaar,
include the windows (seconds) and the basis prices in the resource's service
metadata. **Open:** field names to align with the Bazaar maintainers.

## Security considerations

- **Replay:** each gate is a separate `exact` authorization with its own nonce.
  The unlock resource URL MUST include the `work_id`, so an unlock authorization
  cannot be replayed against another work item.
- **Authorization scope:** the unlock payer SHOULD be the admission payer. A
  server MAY enforce this. Verifi binds both to the requester wallet.
- **Clock and state manipulation:** the seller controls `ready_at`. A dishonest
  seller could hold a finished result to move it into a later window, but the
  only possible effect is a *lower* price, so the incentive runs against abuse.
  The reverse, back-dating `ready_at` to claim the SLA price, cannot be prevented
  by the protocol alone. The buyer can at least bound it: a result cannot have
  been ready before the buyer's last poll that returned `processing`. Clients
  SHOULD keep that evidence. Servers SHOULD emit a receipt through
  `offer-and-receipt` that includes `ready_at` and the applied window, which makes
  the seller's claim attributable and disputable.
- **Exchange-rate gaming:** a buyer could collect quotes and pay against the most
  favourable one. `terms_valid_until` limits this. A server that converts between
  currencies SHOULD keep quotes short-lived and SHOULD round converted amounts
  up.
- **Queue flooding:** the admission price is the primary control. Servers SHOULD
  also cap active work items per payer and in total, and SHOULD apply those caps
  after the payment gate but refuse without charging.
- **Settlement atomicity:** a work item MUST NOT enter the human queue before its
  admission settlement is recorded.

## Humane operation (non-normative)

- Windows describe the pool, not an individual. A single responder SHOULD NOT be
  offered at a window that requires round-the-clock availability.
- The grace window exists so that a person may rest. Sellers are encouraged to
  choose grace windows that match human rhythms (for example 24 hours) rather than
  the shortest achievable time.
- Showing responders the SLA deadline is useful; ranking or penalising them for
  grace-window answers defeats the purpose of the extension.
- A responder's pay that follows the price actually paid keeps the incentives
  honest and visible. Verifi pays responders from the unlock price of the applied
  window.

## Example parameter sets

| Deployment | Admission | SLA window | SLA price | Grace window | Grace price |
|---|---|---|---|---|---|
| Single expert (Verifi contract v3) | 0.10 EUR | 60 min | 2.90 EUR | 60 min to 24 h | 1.45 EUR (−50 %) |
| 24/7 pool of 100 people | set by operator | 10 min | set by operator | 10 to 30 min | set by operator |

Verifi accepts EURC at the euro prices and USDC at the converted amount locked at
admission, and does not allow cancellation.

## Open questions

1. **Name.** `service-windows` is descriptive. Alternatives: `human-latency`,
   `timed-fulfilment`.
2. **Unlock deadline.** Should a ready result expire if the buyer never unlocks
   it? Otherwise, how long must the server retain it?
3. **More than one grace tier.** This draft allows exactly one. A stepped or
   continuous discount curve is possible, but it costs client simplicity.
4. **Settlement binding.** `exact` per gate keeps each payment simple and
   independently verifiable. An alternative authorises the maximum unlock at
   admission with `auth-capture` or `upto`, then captures the window price. That
   saves a round trip but locks buyer funds for up to the grace window.
5. **Buyer deadline.** Should a buyer be able to declare, at admission, that it
   accepts only the SLA window, so that the work item expires at the SLA deadline
   instead of continuing into the grace window?
6. **Admission credit portability.** Should `expiry.admission_credit` be
   standardised further, or stay seller-defined?

## Appendix A: `info` schema (sketch)

```json
{
  "type": "object",
  "required": ["version", "terms_id", "windows", "prices", "asset_binding", "expiry", "cancellation"],
  "properties": {
    "version": { "const": 1 },
    "terms_id": { "type": "string" },
    "terms_valid_until": { "type": "string", "format": "date-time" },
    "windows": {
      "type": "object",
      "required": ["sla"],
      "properties": {
        "sla":   { "type": "object", "required": ["within_seconds"],
                   "properties": { "within_seconds": { "type": "integer", "minimum": 1 } } },
        "grace": { "type": "object", "required": ["within_seconds"],
                   "properties": { "within_seconds": { "type": "integer", "minimum": 1 } } }
      }
    },
    "price_basis": {
      "type": "object",
      "required": ["currency", "admission", "sla"],
      "properties": {
        "currency": { "type": "string" },
        "admission": { "type": "string" },
        "sla": { "type": "string" },
        "grace": { "type": "string" }
      }
    },
    "prices": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "required": ["network", "asset", "admission", "sla"],
        "properties": {
          "network": { "type": "string" },
          "asset": { "type": "string" },
          "admission": { "type": "string", "pattern": "^[0-9]+$" },
          "sla": { "type": "string", "pattern": "^[0-9]+$" },
          "grace": { "type": "string", "pattern": "^[0-9]+$" },
          "conversion": {
            "type": "object",
            "required": ["from", "rate", "source", "as_of"],
            "properties": {
              "from": { "type": "string" },
              "rate": { "type": "string" },
              "source": { "type": "string" },
              "as_of": { "type": "string" },
              "rounding": { "enum": ["up_to_cent", "none"] }
            }
          }
        }
      }
    },
    "asset_binding": { "enum": ["admission_asset"] },
    "expiry": {
      "type": "object",
      "properties": {
        "admission_credit": { "enum": ["next_admission", "none"] }
      }
    },
    "cancellation": { "enum": ["none", "after_sla_admission_credit"] },
    "status_url_template": { "type": "string" },
    "unlock_url_template": { "type": "string" }
  }
}
```

## Submission plan (for the author, not part of the spec)

1. Open a GitHub issue in `x402-foundation/x402`: problem, high-level approach,
   and why `exact`, `upto`, `auth-capture` and `offer-and-receipt` do not cover it.
   Link to Verifi as a live service. Share it in the x402 Slack (slack.x402.org).
2. After feedback, open a spec-only PR adding
   `specs/extensions/extension-service-windows.md`. Keep the text short: the
   foundation's contribution guide asks for concise, human-reviewed output and
   disclosure of significant AI assistance. Commits must be signed.
3. Ship Verifi contract v3 as the running reference implementation, then offer an
   SDK implementation in one language after the spec is accepted.
