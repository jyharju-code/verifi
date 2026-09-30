# The money architecture, in plain language

How money moves through Verifi, which wallets exist, where the keys live,
and why the design is safe.

## Four wallets, four roles

A wallet is an account: the public address (0x...) is the account number,
the private key is the signing right. Whoever holds the key controls the
account.

| Wallet | Whose | What it holds | Where the key lives |
|---|---|---|---|
| Agent wallet | The customer's | The customer's USDC or EURC | With the customer, never with us |
| Receiving wallet | The operator's | Revenue | With the operator (for example an exchange account); the service only knows the address |
| Gas wallet | The service's | A few euros of ETH | On the server and in the operator's encrypted keychain |
| Test buyer | The service's | A few USDC for testing | Only in the operator's encrypted keychain |

## The signed cheque and the postman who pays the stamp

An x402 payment works like a cheque, but digital and settled in seconds:

1. When an agent without an entitlement calls the API, the service replies
   with the gate 1 invoice: 0.10 EUR, payable in EURC or in USDC at the
   converted amount, to this address.
2. The agent writes the "cheque": a digitally signed payment authorization
   (EIP-3009) carrying the exact amount, the exact recipient, and a validity
   window. The signature covers all of it, so nobody can alter the amount or
   redirect the recipient afterwards, and the authorization is valid once.
3. The agent retries the same request with the cheque attached.
4. The service's own "postman", the facilitator, checks the cheque and
   submits it to the blockchain for settlement. Settlement costs a small
   processing fee (gas), which the postman pays from its own till, the gas
   wallet. That is why the customer needs no ETH at all: USDC or EURC
   alone is enough.
5. The tokens move directly from the agent's wallet to the operator's
   receiving address. The money never passes through the service and never
   stops in any intermediate account.
6. Only after the gate 1 settlement is recorded does the request continue to
   a human in Telegram.
7. When the human result is ready, the service issues a separate unlock
   invoice in the same token: 2.90 EUR if the human answered within 60
   minutes of admission, 1.45 EUR if later. A new authorization and
   settlement unlocks the result. The two transactions share one verify id
   but are not one payment.

One settlement costs fractions of a cent on Base, so a 15 euro gas till
covers thousands of payments.

## Why this is safe

**There is nothing valuable to steal on the server.** The only private key
on the server belongs to the gas wallet, which holds a small processing-fee
till. A full server compromise loses at most that till. Revenue is never on
the server: it settles on-chain directly to the receiving address, whose
keys exist nowhere in the system.

**The cheque cannot be forged or redirected.** The authorization signature
covers the amount and the recipient. The facilitator cannot route funds to
itself, because the authorization is only valid for the exact transfer the
customer signed, and only once.

**Keys live in two protected places.** The gas wallet key sits in the
server's .env file, which is immutable at the filesystem level outside the
ops wrapper and never enters version control. A backup lives in the
operator's encrypted OS keychain. Both wallets were generated
programmatically, so the keys have never passed through a browser.

**Every transaction is recorded twice.** The blockchain itself is a public
receipt for every transfer, and PostgreSQL records each entry settlement,
unlock settlement, price, wallet, free entitlement, credit, and lifecycle
event. The append-only audit log records every money-related transition.

## What happens when an agent pays for one complete chain

1. Agent: POST /verify with the claim.
2. Service: 402 Payment Required with the invoice.
3. Agent signs the payment authorization with its own key. No gas, no ETH.
4. Agent repeats the request with the authorization attached.
5. The facilitator verifies it and submits the transfer; gas comes from the
   gas wallet.
6. The admission, for example 0.10 EURC, moves from the agent directly to
   the receiving address.
7. The durable verify id returns and the agent polls while a human works.
8. Polling reports `ready`, with the answer still locked.
9. The agent calls the unlock endpoint, signs a new authorization for the
   SLA or grace price in the same token, and the facilitator settles the
   second transaction.
10. The completed response contains the answer. Total charged is 3.00 EURC
    for an SLA answer, 1.55 EURC for a grace answer (USDC: the converted
    amounts).

## No free chains, only earned failure credits

There is no free human work: every chain pays its admission and its unlock
(`verify-api.free_tier_count` is `0`). The only way a gate is covered without a
fresh payment is an earned credit: if an admitted chain whose admission was
actually paid expires without an answer, the wallet gets one admission-only
credit in the same token. It replaces the next admission payment but not the
later unlock. Because credits come only from a paid, failed chain, they cannot be
farmed for free work.

## Turning on mainnet settlement

The postman has to serve the chain the prices are quoted in. The default
`FACILITATOR_URL=https://x402.org/facilitator` is testnet only: it serves
scheme `exact` on `eip155:84532` (Base Sepolia), never on the `eip155:8453`
mainnet in `X402_NETWORK`. With that pair, both paid gates refuse every
request and verify-api logs `FACILITATOR MISMATCH` at startup. Admissions that
need no fresh payment, such as an entry funded by an earned credit, keep
working, because they never touch a facilitator.

The fix is the self-hosted facilitator already defined in
`deploy/docker-compose.yml` under the `payments` profile, which the `verifi`
wrapper always includes. `deploy/facilitator-config.json` already declares
scheme id `v2-eip155-exact` on `eip155:8453`, and that id is reported to
verify-api as scheme `exact` at x402 version 2, which is exactly what the
paid routes register.

One command does the whole server side (refresh `/usr/local/bin/verifi` from
`scripts/verifi-ops.sh` first if the installed wrapper predates it):

```
verifi payments-setup
```

It generates the gas wallet inside the verify-api image when `.env` has no
`FACILITATOR_PRIVATE_KEY` (an existing key is never overwritten), derives and
stores `FACILITATOR_ADDRESS` for balance monitoring, points
`FACILITATOR_URL` at `http://facilitator:8080` (docker network only, so it
stays off the public surface), deploys the facilitator and verify-api in
that order, confirms via `/supported` that the facilitator serves `exact` on
the configured network, and prints the gas balance. It is idempotent and
audited, and it never writes the key anywhere except `.env`.

The one step no command can do is money: send a few euros of ETH on Base to
the printed gas wallet address, and copy the key from `.env` into the
operator's encrypted keychain. The balance line in the output shows whether
funding has happened. Settlement starts working the moment gas lands, with
no rerun needed.

The result can always be rechecked in the log, which is the whole check:

```
docker logs verifi-verify-api-1 2>&1 | grep -i facilitator
```

`facilitator serves exact on eip155:8453` means settlement works. Another
`FACILITATOR MISMATCH` means the facilitator did not load the config or the
chain is missing from it. Verify it directly with
`curl -s localhost:8402/supported`, which must list scheme `exact` on
`eip155:8453`. `verifi payments-setup` performs this same check and refuses
to point verify-api at a facilitator that fails it.

Until the gas wallet exists, the honest alternative is to keep the paid gates
off. Leaving `X402_PAY_TO` unset makes both gates answer a clear 503 instead
of advertising a price nothing can settle.

## Two tokens: USDC and EURC (contract v3)

Agents pay in USDC or in EURC, both native Circle tokens on Base with
EIP-3009 `transferWithAuthorization`. The facilitator needs no change for
EURC: x402-rs has no token allowlist and signs each transfer with the EIP-712
domain carried in the payment requirement (`EURC`, version `2`). core-api
checks at startup that both token contracts still report the configured
name, version and decimals, and alerts the operator if one does not.

Before switching contract v3 on, confirm that the receiving address accepts
EURC on Base. An exchange deposit address that lists only USDC on Base may
not credit an EURC transfer. The receiving wallet row on the dashboard shows
the USDC balance only; check EURC on the block explorer.

Responder earnings are kept per token and paid out per token: an EURC chain
earns EURC, a USDC chain earns USDC, and nothing is converted. `/payouts`
prints a ready awal command for USDC and a manual instruction for EURC,
because awal sends USDC only.

## Maintenance

- Watch the gas wallet balance and top it up when it approaches a couple of
  euros.
- Watch for `FACILITATOR MISMATCH` in the verify-api log after any change to
  `X402_NETWORK`, `FACILITATOR_URL`, or `facilitator-config.json`.
- If the gas wallet key is ever suspected leaked, generate a new wallet,
  move the till, and swap the key on the server. The receiving address can
  be changed with a single configuration change.
