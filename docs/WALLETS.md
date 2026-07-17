# The money architecture, in plain language

How money moves through Verifi, which wallets exist, where the keys live,
and why the design is safe.

## Four wallets, four roles

A wallet is an account: the public address (0x...) is the account number,
the private key is the signing right. Whoever holds the key controls the
account.

| Wallet | Whose | What it holds | Where the key lives |
|---|---|---|---|
| Agent wallet | The customer's | The customer's USDC | With the customer, never with us |
| Receiving wallet | The operator's | Revenue | With the operator (for example an exchange account); the service only knows the address |
| Gas wallet | The service's | A few euros of ETH | On the server and in the operator's encrypted keychain |
| Test buyer | The service's | A few USDC for testing | Only in the operator's encrypted keychain |

## The signed cheque and the postman who pays the stamp

An x402 payment works like a cheque, but digital and settled in seconds:

1. When an agent calls the API without payment, the service replies with an
   invoice: this costs 0.10 USDC, payable to this address.
2. The agent writes the "cheque": a digitally signed payment authorization
   (EIP-3009) carrying the exact amount, the exact recipient, and a validity
   window. The signature covers all of it, so nobody can alter the amount or
   redirect the recipient afterwards, and the authorization is valid once.
3. The agent retries the same request with the cheque attached.
4. The service's own "postman", the facilitator, checks the cheque and
   submits it to the blockchain for settlement. Settlement costs a small
   processing fee (gas), which the postman pays from its own till, the gas
   wallet. That is why the customer needs no ETH at all: USDC alone is
   enough.
5. The USDC moves directly from the agent's wallet to the operator's
   receiving address. The money never passes through the service and never
   stops in any intermediate account.
6. Only after the settlement is confirmed does the request continue to a
   human in Telegram.

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
receipt for every transfer, and the service's append-only audit log records
every paid verify, price change, and settlement.

## What happens when an agent pays for one verify

1. Agent: POST /verify with the claim.
2. Service: 402 Payment Required with the invoice.
3. Agent signs the payment authorization with its own key. No gas, no ETH.
4. Agent repeats the request with the authorization attached.
5. The facilitator verifies it and submits the transfer; gas comes from the
   gas wallet.
6. 0.10 USDC moves from the agent directly to the receiving address.
7. The verify is created, the card lands in a responder's Telegram, and the
   answer returns to the agent in the same HTTP request (or via polling and
   webhooks if the human takes longer).

## Maintenance

- Watch the gas wallet balance and top it up when it approaches a couple of
  euros.
- If the gas wallet key is ever suspected leaked, generate a new wallet,
  move the till, and swap the key on the server. The receiving address can
  be changed with a single configuration change.
